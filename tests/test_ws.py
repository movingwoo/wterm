"""WebSocket 프로토콜과 PTY 세션 — 이 저장소의 실제 스모크 테스트.

진짜 서버에 진짜 셸을 띄우고 입력·출력·리사이즈·재접속 replay까지 왕복시킨다.
AGENTS.md "Session invariants"와 "WebSocket contract"에 적힌 것들이 대상이다.
"""
from __future__ import annotations

import json
import os
import pwd
import time

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

RECV_TIMEOUT = 20.0


def ws_connect(h, path: str, *, token: str | None = None, origin: str | None = ""):
    """`origin=""`는 기본값(서버 Origin)을, `origin=None`은 헤더 생략을 뜻한다."""
    headers = {}
    if origin == "":
        origin = h.origin
    if origin is not None:
        headers["Origin"] = origin
    if token:
        headers["Cookie"] = f"wterm_token={token}"
    return connect(f"{h.ws_url}{path}", additional_headers=headers, open_timeout=15)


# 재접속에서 replay 버퍼(바이너리)는 status 텍스트보다 **먼저** 도착한다 — 라우트가
# attach()로 버퍼를 흘려보낸 뒤에 status를 보내기 때문이다. status_message가 그 사이의
# 바이너리를 그냥 버리면 뒤따르는 recv_until은 이미 지나간 출력을 기다리다 타임아웃한다.
# 재접속이 빠르면 버퍼가 거의 비어 있어 드러나지 않지만(그래서 오래 안 보였다), 로그아웃
# 왕복처럼 시간이 걸리면 세션 출력 전체가 한 프레임에 실려 사라진다. 건너뛴 출력은
# 소켓에 달아 두고 다음 recv_until이 먼저 소비한다.


def _take_skipped(ws) -> str:
    seen = getattr(ws, "_skipped_output", "")
    ws._skipped_output = ""
    return seen


def recv_until(ws, marker: str, timeout: float = RECV_TIMEOUT) -> str:
    """마커가 나올 때까지 터미널 출력을 모은다. JSON 텍스트 메시지는 건너뛴다."""
    seen = _take_skipped(ws)
    if marker in seen:
        return seen
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
        if isinstance(msg, bytes):
            seen += msg.decode("utf-8", "replace")
            if marker in seen:
                return seen
    raise AssertionError(f"{timeout}초 안에 {marker!r}를 보지 못함. 받은 출력:\n{seen}")


def send_line(ws, line: str) -> None:
    ws.send(json.dumps({"type": "input", "data": line + "\n"}))


def end_shell(ws, timeout: float = RECV_TIMEOUT) -> None:
    """`exit`로 셸을 정상 종료시키고 exit 알림까지 확인한다.

    정리 시간도 같이 줄어든다 — 대화형 bash는 SIGTERM을 무시하므로, 세션을
    살려둔 채 서버를 내리면 종료 경로가 SIGKILL까지 SIGTERM_WAIT(10초)를 꽉 채운다.

    `true`를 먼저 보내는 이유: 인자 없는 `exit`는 `$?`를 그대로 반환하는데, 그
    시점의 `$?`는 셸 시작 파일이 남긴 값이다. macOS `/etc/bashrc`의 마지막 줄이
    `[ -r "/etc/bashrc_$TERM_PROGRAM" ] && . ...` 이고, 데몬처럼 TERM_PROGRAM이
    없는 환경에서는 이 줄이 거짓이라 rc 파일이 1을 남긴 채 끝난다 (로그인 셸만
    /etc/profile → /etc/bashrc를 타므로 평소 터미널에서는 드러나지 않는다).
    검사하려는 것은 서버가 자식의 종료 코드를 그대로 전달하는가이지 남의 rc
    파일 내용이 아니므로, 시작 상태를 0으로 맞춰 놓고 종료시킨다.
    """
    send_line(ws, "true")
    send_line(ws, "exit")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
        if isinstance(msg, bytes):
            continue
        payload = json.loads(msg)
        if payload["type"] == "exit":
            assert payload["code"] == 0
            return
    raise AssertionError("셸 종료 후 exit 알림이 오지 않았다")


def status_message(ws) -> str:
    msg = ws.recv(timeout=RECV_TIMEOUT)
    while isinstance(msg, bytes):  # 셸 출력이나 replay 버퍼가 먼저 오는 경우
        ws._skipped_output = _take_skipped(ws) + msg.decode("utf-8", "replace")
        msg = ws.recv(timeout=RECV_TIMEOUT)
    payload = json.loads(msg)
    assert payload["type"] == "status"
    return payload["message"]


# ── 접속 거부 경로 ───────────────────────────────────────────────────
#
# 오리진 검사만 ws.accept() 전에 닫는다. Starlette이 핸드셰이크를 HTTP 403으로
# 끝내 close code가 전달되지 않는데, 4403은 그것이 의도한 동작이다 — 공격자
# 페이지가 열린 소켓을 단 한 순간도 쥐지 못하는 쪽이 낫고 원인은 서버 로그에
# 남는다. 나머지 거절은 accept 뒤라 close code가 그대로 도착한다.


@pytest.mark.parametrize(
    "origin",
    [
        None,                       # Origin 없음
        "https://evil.example",     # 다른 사이트
    ],
)
def test_bad_origin_fails_handshake(start_server, origin):
    h = start_server()
    token = h.login()
    with pytest.raises(InvalidStatus) as exc:
        ws_connect(h, "/ws/demo", token=token, origin=origin)
    assert exc.value.response.status_code == 403


@pytest.mark.parametrize(
    "path, code",
    [
        ("/ws/not-whitelisted", 4404),   # 화이트리스트 밖
        ("/ws/demo?agent=bogus", 4400),  # 지원하지 않는 에이전트
    ],
)
def test_rejected_after_accept_delivers_close_code(start_server, path, code):
    """close code가 도착해야 클라이언트가 원인을 알고 재연결을 멈춘다."""
    h = start_server()
    token = h.login()
    with ws_connect(h, path, token=token) as ws:
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=RECV_TIMEOUT)
    assert exc.value.rcvd.code == code


def test_unauthenticated_ws_closes_with_4401(start_server):
    """인증 실패는 클라이언트가 재로그인 화면을 띄울 수 있게 4401로 알린다."""
    h = start_server()
    with ws_connect(h, "/ws/demo") as ws:
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=RECV_TIMEOUT)
    assert exc.value.rcvd.code == 4401


def test_unauthenticated_unknown_project_still_says_4401(start_server):
    """인증 없는 상대에게 화이트리스트 내용을 알려주지 않는다 (4404가 아니라 4401)."""
    h = start_server()
    with ws_connect(h, "/ws/not-whitelisted") as ws:
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=RECV_TIMEOUT)
    assert exc.value.rcvd.code == 4401


# ── PTY 왕복 ─────────────────────────────────────────────────────────


def test_shell_session_roundtrip(start_server):
    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        assert "셸" in status_message(ws)

        # 입력이 실제로 실행된다 (에코된 입력 자체와 구별되는 출력을 쓴다).
        send_line(ws, "echo WTERM-$((21 * 2))-OK")
        recv_until(ws, "WTERM-42-OK")

        # 리사이즈가 PTY의 winsize까지 내려간다.
        ws.send(json.dumps({"type": "resize", "cols": 100, "rows": 40}))
        send_line(ws, "stty size")
        recv_until(ws, "40 100")

        status = h.client(headers={"Origin": h.origin, "Cookie": f"wterm_token={token}"})
        (demo,) = status.get("/api/projects").json()
        assert demo["shell_live"] is True
        assert demo["live"] is False  # claude 세션과 키가 분리되어 있다

        end_shell(ws)


def test_malformed_messages_do_not_kill_the_session(start_server):
    """프로토콜 경계에서 잘못된 메시지는 조용히 무시되어야 한다.

    걸러지지 않으면 예외가 수신 루프 밖으로 나가 소켓이 닫힌다. 자기 세션만
    끊는 자해라 보안 문제는 아니지만, 버그난 클라이언트는 원인 모를 단절을 겪고
    서버 로그에는 트레이스백만 쌓인다. 잘못된 JSON은 원래 무시하고 있었으므로
    나머지도 같은 규칙으로 맞춘 것이다.
    """
    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        for bad in (
            "이건 JSON이 아니다",                                   # JSON이 아님
            "5",                                                    # JSON이지만 객체가 아님
            b"\x00\x01\x02",                                        # 바이너리 프레임
            json.dumps({"type": "resize"}),                         # cols/rows 없음
            json.dumps({"type": "resize", "cols": "80", "rows": "24"}),  # 숫자가 아님
            json.dumps({"type": "resize", "cols": 99999, "rows": 24}),   # winsize 범위 밖
            json.dumps({"type": "input", "data": {"nope": 1}}),      # data가 문자열이 아님
            json.dumps({"type": "없는타입"}),
        ):
            ws.send(bad)

        # 위 리사이즈가 struct.error를 냈다면 이 시점에 소켓은 이미 닫혀 있다.
        ws.send(json.dumps({"type": "resize", "cols": 100, "rows": 40}))
        send_line(ws, "stty size")
        recv_until(ws, "40 100")
        end_shell(ws)


def test_reconnect_replays_buffer(start_server):
    """유예 시간 안에 다시 붙으면 같은 프로세스에 화면까지 복원된다."""
    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        send_line(ws, "echo BEFORE-DETACH")
        recv_until(ws, "BEFORE-DETACH")

    with ws_connect(h, "/ws/demo?agent=shell&mode=attach", token=token) as ws:
        assert "재접속" in status_message(ws)
        recv_until(ws, "BEFORE-DETACH")  # replay 버퍼
        send_line(ws, "echo AFTER-REATTACH")
        recv_until(ws, "AFTER-REATTACH")
        end_shell(ws)


def test_second_client_replaces_first(start_server):
    """세션당 붙을 수 있는 WebSocket은 하나. 밀려난 쪽은 4000으로 닫힌다."""
    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as first:
        status_message(first)
        with ws_connect(h, "/ws/demo?agent=shell", token=token) as second:
            status_message(second)
            with pytest.raises(ConnectionClosed) as exc:
                while True:
                    first.recv(timeout=RECV_TIMEOUT)
            assert exc.value.rcvd.code == 4000

            send_line(second, "echo SECOND-OWNS-IT")
            recv_until(second, "SECOND-OWNS-IT")
            end_shell(second)


def test_login_shell_falls_back_to_passwd_entry(start_server):
    """$SHELL이 없는 환경(=데몬)에서도 사용자의 로그인 셸을 띄운다.

    launchd/systemd는 로그인 셸을 거치지 않아 $SHELL을 주지 않는다. 환경변수만
    보면 zsh 사용자가 **부팅 자동 기동일 때만** bash를 받게 되는데, 그 차이는
    손으로 띄워서는 절대 재현되지 않는다. 그래서 passwd 엔트리에서 직접 읽는다.
    """
    expected = pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    h = start_server(env={"SHELL": None})
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        # argv[0]이 곧 띄운 셸의 경로다. 입력 에코와 구별되도록 계산식을 섞는다.
        send_line(ws, 'echo "SH$((1 + 1))-IS:$0:"')
        recv_until(ws, f"SH2-IS:{expected}:")
        end_shell(ws)


def test_missing_agent_binary_reports_exit(start_server):
    """claude/codex가 PATH에 없는 환경(데몬에서 흔한 사고)에서 exit로 알려준다."""
    h = start_server(env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=claude&mode=new", token=token) as ws:
        deadline = time.monotonic() + RECV_TIMEOUT
        while time.monotonic() < deadline:
            msg = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            if isinstance(msg, bytes):
                continue
            payload = json.loads(msg)
            if payload["type"] == "exit":
                assert payload["code"] == 127  # execvpe 실패
                return
        raise AssertionError("exit 메시지가 오지 않았다")


def test_legacy_shell_query_param(start_server):
    """예전 URL(`shell=1`)은 계속 동작해야 한다."""
    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?shell=1", token=token) as ws:
        assert "셸" in status_message(ws)
        send_line(ws, "echo LEGACY-OK")
        recv_until(ws, "LEGACY-OK")
        end_shell(ws)


# ── 유휴 종료 ────────────────────────────────────────────────────────


def test_idle_session_is_closed_and_reaped(start_server, project_dir):
    """탭을 열어둔 채 잊은 세션은 grace가 잡지 못한다 — 그건 연결이 끊겨야 도는
    타이머다. idle_seconds가 켜져 있으면 조용한 세션이 스스로 정리되어야 하고,
    그때 자식 프로세스까지 실제로 죽어야 한다 (닫힌 소켓만으로는 아무것도 안 끝난다).

    4408로 닫는 이유는 app.js의 자동 재연결 때문이다. 평범한 단절로 보이면
    방금 정리한 세션이 곧바로 다시 뜬다.
    """
    pid_marker = project_dir / "idle-shell.pid"  # 셸의 cwd는 프로젝트 경로다
    pid_marker.unlink(missing_ok=True)

    h = start_server(idle_seconds=3)
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        send_line(ws, f"echo $$ > {pid_marker.name}")
        recv_until(ws, pid_marker.name)
        child_pid = _read_pid(pid_marker)

        # 여기서부터 양쪽 다 조용하다. 서버가 먼저 말을 걸어와야 한다.
        with pytest.raises(ConnectionClosed) as exc:
            while True:
                msg = ws.recv(timeout=RECV_TIMEOUT)
                if isinstance(msg, str):
                    payload = json.loads(msg)
                    assert payload["type"] == "status"
                    assert "종료" in payload["message"]
    assert exc.value.rcvd.code == 4408

    # 대화형 bash는 SIGTERM을 무시하므로 SIGKILL까지 SIGTERM_WAIT(10초)가 걸린다.
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    raise AssertionError(f"유휴 종료 후에도 자식 프로세스 {child_pid}가 살아 있다")


def test_activity_keeps_a_session_alive(start_server):
    """사람 없이 오래 도는 자동 실행을 유휴로 잡아 죽이는 쪽이 훨씬 비싼 오답이다.
    그래서 활동은 입력과 출력 **양쪽**으로 세고, 어느 쪽이든 있으면 타이머가 선다."""
    h = start_server(idle_seconds=4)
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        for i in range(3):  # 4.5초 — 리셋이 없으면 이 사이에 죽는다
            time.sleep(1.5)
            send_line(ws, f"echo STILL-HERE-{i}")
            recv_until(ws, f"STILL-HERE-{i}")
        end_shell(ws)


def _read_pid(path, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError(f"셸이 {timeout}초 안에 {path}를 쓰지 않았다")


def test_agents_have_independent_sessions(start_server):
    """세션 키는 프로젝트#에이전트. claude와 셸이 서로를 밀어내지 않는다."""
    h = start_server(env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as shell_ws:
        status_message(shell_ws)
        send_line(shell_ws, "echo SHELL-LIVE")
        recv_until(shell_ws, "SHELL-LIVE")

        # claude는 즉시 죽지만(바이너리 없음) 셸 세션은 영향을 받지 않는다.
        with ws_connect(h, "/ws/demo?agent=claude", token=token) as claude_ws:
            for _ in range(10):
                if isinstance(claude_ws.recv(timeout=RECV_TIMEOUT), str):
                    break

        send_line(shell_ws, "echo SHELL-STILL-LIVE")
        recv_until(shell_ws, "SHELL-STILL-LIVE")
        end_shell(shell_ws)
