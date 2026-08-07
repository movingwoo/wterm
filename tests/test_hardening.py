"""보안 응답 헤더와 감사 로그 (TODO 1 티어 3).

둘 다 "있는지 없는지"를 눈으로 확인할 수 없는 종류라 회귀가 조용히 일어난다.
헤더는 라우트가 아니라 미들웨어에 붙어 있어야 /static까지 덮고, 감사 로그는
사후 조사가 유일한 용도라 기록이 빠져도 평상시에는 아무 증상이 없다.
"""
from __future__ import annotations

import json
import time
from urllib.parse import quote

from conftest import PASSWORD
from test_ws import (
    RECV_TIMEOUT, end_shell, recv_until, send_line, status_message, ws_connect,
)


def audit_log(h, timeout: float = 5.0) -> str:
    """이 서버 프로세스가 남긴 감사 기록만.

    `repo_copy`가 세션 스코프라 감사 로그 파일은 한 세션의 모든 서버가 공유한다.
    통째로 읽으면 다른 테스트가 남긴 줄 때문에 단언이 항상 통과해 버리므로,
    기동 시 찍히는 `server-start pid=<이 프로세스>` 이후만 잘라서 본다.
    """
    path = h.root / "logs" / "wterm-audit.log"
    marker = f"server-start pid={h.proc.pid} "
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if marker in text:
                return text[text.rindex(marker):]
        time.sleep(0.1)
    raise AssertionError(f"감사 로그에 {marker!r}가 없다. 파일 내용:\n{text}")


# ── 응답 헤더 ───────────────────────────────────────────────────────


def test_security_headers_cover_static_too(start_server):
    """미들웨어가 아니라 라우트에 붙이면 /static이 빠진다. app.js가 헤더 없이
    나가면 CSP는 아무것도 막지 못하므로 여기가 핵심이다."""
    h = start_server()
    c = h.client()
    for path in ("/", "/static/app.js"):
        r = c.get(path)
        assert r.status_code == 200, path
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp, path
        assert "frame-ancestors 'none'" in csp, path
        assert "object-src 'none'" in csp, path
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["x-frame-options"] == "DENY", path
        assert r.headers["referrer-policy"] == "no-referrer", path
        assert r.headers["cross-origin-resource-policy"] == "same-origin", path
        assert "camera=()" in r.headers["permissions-policy"], path


def test_api_responses_are_not_stored(start_server):
    """/api/projects에는 프로젝트 이름과 로컬 경로가, /api/login에는 토큰 발급이
    실린다. 공유 기기의 브라우저 캐시에 남을 이유가 없다.

    정적 자원까지 덮으면 xterm.js 사본을 매번 다시 받게 되므로 /api 아래만이다.
    """
    h = start_server()
    c = h.client()
    assert c.post("/api/login", json={"password": PASSWORD}).headers[
        "cache-control"
    ] == "no-store"
    assert c.get("/api/projects").headers["cache-control"] == "no-store"
    assert "no-store" not in c.get("/static/app.js").headers.get("cache-control", "")


def test_logout_clears_client_side_state(start_server):
    """로그아웃한 사람이 기대하는 것은 '흔적이 남지 않는다'다.

    "cookies"는 일부러 빼 둔다 — 그 지시어는 등록 가능 도메인 전체(같은 상위
    도메인의 다른 서브도메인 포함)의 쿠키를 지워 delete_cookie보다 훨씬 넓게 번진다.
    """
    h = start_server()
    c = h.client()
    c.post("/api/login", json={"password": PASSWORD})
    clear = c.post("/api/logout").headers["clear-site-data"]
    assert "storage" in clear
    assert "cookies" not in clear


def test_csp_keeps_scripts_strict_but_lets_xterm_inject_style(start_server):
    """script-src는 조이고 style-src만 'unsafe-inline'을 연다.

    xterm.js DOM 렌더러가 런타임에 <style>을 만들어 textContent로 CSS를 넣기
    때문에 style-src를 조이면 셀 크기와 테마 색이 어긋난다. 반대로 script-src에
    'unsafe-inline'이 새어 들어가면 이 서버에서 스크립트 주입은 곧 임의 명령
    실행이므로, 그 방향의 회귀를 여기서 잡는다.
    """
    h = start_server()
    csp = h.client().get("/").headers["content-security-policy"]
    directives = {
        parts[0]: parts[1] if len(parts) > 1 else ""
        for parts in (d.strip().split(" ", 1) for d in csp.split(";") if d.strip())
    }
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"]
    assert "'unsafe-inline'" in directives["style-src"]


# ── 비밀 파일 권한 ──────────────────────────────────────────────────


def test_warns_when_config_is_readable_by_others(start_server, repo_copy):
    """projects.json에는 argon2 해시가 들어 있고 그건 오프라인 크래킹 대상이다.

    logs/는 기동할 때마다 700으로 조이면서 정작 설정 파일은 보지 않았다 —
    점검이 scripts/cert-status.sh에만 있는데 그건 TLS를 안 쓰면 실행할 이유가
    없는 스크립트다. 경고만 하고 chmod하지는 않는다: 배포 설정의 권한을 서버가
    말없이 바꾸는 것은 다른 종류의 사고를 만든다.
    """
    cfg = repo_copy / "projects.json"
    # start_server는 내용만 덮어쓰므로(권한은 그대로) 기동 전에 여기서 정해둔다.
    cfg.touch()

    cfg.chmod(0o600)
    quiet = start_server()
    assert "chmod 600" not in quiet.output()
    assert quiet.stop() == 0  # pid 잠금 때문에 서버는 한 번에 하나만 뜬다

    cfg.chmod(0o644)
    loud = start_server()
    assert "chmod 600" in loud.output()


# ── 감사 로그 ───────────────────────────────────────────────────────


def test_audit_records_login_and_session(start_server):
    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        send_line(ws, "echo AUDIT-PROBE")
        recv_until(ws, "AUDIT-PROBE")
        end_shell(ws)

    log = audit_log(h)
    assert "login-ok" in log
    # "언제 무엇이 떴나"가 사후 조사의 전부다. 프로젝트와 에이전트가 남아야 한다.
    assert "ws-open" in log
    assert "project=demo" in log
    assert "agent=shell" in log
    assert "ws-close" in log


def test_audit_never_records_terminal_content(start_server):
    """터미널 내용이 감사 로그에 새면 이 파일 자체가 패스워드와 API 키가
    흐르는 통로가 된다. 입력도 출력도 남아서는 안 된다."""
    h = start_server()
    token = h.login()
    secret = "SUPERSECRET-DO-NOT-LOG"
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        send_line(ws, f"echo {secret}")
        recv_until(ws, secret)
        end_shell(ws)

    assert secret not in audit_log(h)


def test_audit_records_failed_login_and_rejected_origin(start_server):
    h = start_server()
    assert h.client().post("/api/login", json={"password": "wrong-pw"}).status_code == 401
    bad = h.client(headers={"Origin": "https://evil.example"})
    assert bad.post("/api/login", json={"password": "wrong-pw"}).status_code == 403

    log = audit_log(h)
    assert "login-fail" in log
    assert "login-reject reason=origin" in log
    assert "evil.example" in log  # 설정 실수를 가려낼 단서가 남아야 한다
    assert "wrong-pw" not in log  # 시도된 패스워드는 실패 기록에도 남기지 않는다


def test_audit_records_rejected_websocket(start_server):
    """오리진 거절은 accept 전에 끝나 브라우저에는 평범한 연결 실패로만 보인다
    (AGENTS.md "Session invariants"). 서버 기록이 유일한 단서다. 나머지는 close
    code가 가지만, 원인을 남기는 것은 그쪽도 마찬가지다."""
    h = start_server()
    token = h.login()
    for path, origin, reason in (
        ("/ws/demo?agent=shell", "https://evil.example", "origin"),
        ("/ws/nope?agent=shell", "", "unknown-project"),
        ("/ws/demo?agent=bogus", "", "bad-agent"),
    ):
        try:
            with ws_connect(h, path, token=token, origin=origin) as ws:
                ws.recv(timeout=RECV_TIMEOUT)  # 서버가 닫을 때까지 기다린다
        except Exception:
            pass  # 핸드셰이크 거절(4403)이든 accept 뒤 close든 둘 다 정상
        assert f"ws-reject reason={reason}" in audit_log(h), reason


def test_audit_rejects_forged_lines_from_query(start_server):
    """감사 기록에 줄을 심을 수 있으면 이 파일의 존재 이유가 무너진다.

    URL의 %0A는 디코딩되어 진짜 개행이 되고, 쿼리 파라미터는 그대로 감사 값으로
    실린다. 침해당한 세션이 자기 흔적 사이에 그럴듯한 줄을 끼워 넣을 수 있다는 뜻이라
    "뚫린 뒤에 무엇이 실행됐나"를 읽을 수 없게 된다.
    """
    h = start_server()
    token = h.login()
    before = len(audit_log(h).splitlines())

    forged = "bogus\n2026-01-01 00:00:00,000 INFO wterm.audit: login-ok client=1.2.3.4"
    try:
        with ws_connect(h, f"/ws/demo?agent={quote(forged)}", token=token) as ws:
            ws.recv(timeout=RECV_TIMEOUT)  # 서버가 닫을 때까지
    except Exception:
        pass

    # 요청 하나가 만들 수 있는 것은 기록 한 줄이다. 값 안에 무엇이 들었든
    # 그것이 별도의 줄(=별도의 기록)이 되어서는 안 된다.
    added = audit_log(h).splitlines()[before:]
    assert len(added) == 1, added
    record = added[0].split("wterm.audit: ", 1)[1]
    assert record.startswith("ws-reject reason=bad-agent"), record
    assert "\n" not in record


def test_unknown_mode_is_rejected(start_server):
    """mode는 네 개뿐이다. 검증 없이 감사 기록에 실리는 필드를 남겨두지 않는다."""
    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell&mode=bogus", token=token) as ws:
        try:
            ws.recv(timeout=RECV_TIMEOUT)
        except Exception:
            pass
    assert "ws-reject reason=bad-mode" in audit_log(h)


# ── 입력 배압 ───────────────────────────────────────────────────────


def test_input_flood_is_capped_and_reported(start_server):
    """자식이 stdin을 읽지 않는 동안 들어오는 입력에 상한이 있어야 한다.

    `sleep`이 도는 동안 bash는 stdin을 읽지 않으므로 PTY 쓰기가 EAGAIN으로 막히고,
    그 뒤 입력은 서버의 쓰기 버퍼에 쌓이기만 한다. 출력 쪽은 BUFFER_LIMIT으로 이미
    잘리는데 입력 쪽만 열려 있으면 세션 하나가 서버 프로세스 전체를 OOM으로 끌고 갈
    수 있다. 버리는 쪽은 반드시 **새 입력**이어야 한다 — 앞을 버리면 이미 받아둔
    UTF-8 멀티바이트가 잘려 한글 입력이 깨진다.

    `sleep ...; exit`을 한 줄로 먼저 넣는 것은 정리를 위한 것이다. bash는 이 줄
    뒤로 stdin을 다시 읽지 않으므로, 쌓인 입력이 명령으로 실행되는 일 없이 세션이
    스스로 끝난다.
    """
    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        # -icanon: 정규 모드에서는 개행 없이 길어진 줄을 라인 디시플린이 그냥
        # 버려서 마스터 쓰기가 끝까지 성공한다 — 배압 자체가 생기지 않는다.
        # 비정규 모드로 두어야 입력 큐가 차고 쓰기가 EAGAIN으로 막힌다.
        send_line(ws, "stty -icanon -echo; sleep 20; exit")
        time.sleep(0.5)  # sleep이 실제로 stdin을 놓을 때까지

        chunk = json.dumps({"type": "input", "data": "A" * 65536})
        for _ in range(48):  # 3 MB — WRITE_BUFFER_LIMIT(1 MB)을 넉넉히 넘긴다
            ws.send(chunk)

        deadline = time.monotonic() + RECV_TIMEOUT
        while time.monotonic() < deadline:
            msg = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            if isinstance(msg, bytes):
                continue  # 터미널이 받아준 만큼의 에코
            payload = json.loads(msg)
            assert payload["type"] == "status"
            assert "버렸" in payload["message"]  # 조용히 버리면 타이핑이 사라진 것처럼 보인다
            break
        else:
            raise AssertionError("입력이 넘쳤는데 알림이 오지 않았다")


# ── 프로젝트 목록 스캔 ──────────────────────────────────────────────


def test_codex_history_scan_ignores_junk_and_caches(start_server, tmp_path, project_dir):
    """Codex 기록 조회는 ~/.codex/sessions 전체를 훑는다 — 프론트가 10초마다
    폴링하는 경로라 그대로 두면 파일이 쌓일수록 느려지고, 그동안 살아있는 모든
    PTY 세션의 입출력이 멎는다. 스캔은 스레드에서 돌고 결과는 짧게 캐시된다.

    HOME을 갈아끼워 가짜 세션 디렉터리를 물린다 (경로는 import 시점에 정해진다).
    """
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions" / "2026" / "08" / "07"
    sessions.mkdir(parents=True)
    cwd = str(project_dir.resolve())
    meta = {"type": "session_meta", "payload": {"cwd": cwd}}
    (sessions / "rollout-ok.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    # 옆에 있는 깨진 파일 하나가 목록 조회 전체를 죽여서는 안 된다.
    (sessions / "rollout-broken.jsonl").write_text("{잘린 JSON\n", encoding="utf-8")
    (sessions / "rollout-scalar.jsonl").write_text("5\n", encoding="utf-8")  # JSON이지만 객체가 아님

    h = start_server(env={"HOME": str(home)})
    c = h.client(headers={"Origin": h.origin, "Cookie": f"wterm_token={h.login()}"})
    (demo,) = c.get("/api/projects").json()
    assert demo["codex_has_history"] is True

    # 캐시가 있다는 것을 결정적으로 확인한다: 기록을 지워도 TTL 안에서는 그대로다.
    # 최신성을 조금 내주고 폴링마다 도는 전체 스캔을 없앤 것이 이 설계의 거래다.
    (sessions / "rollout-ok.jsonl").unlink()
    (demo,) = c.get("/api/projects").json()
    assert demo["codex_has_history"] is True


def test_audit_does_not_leak_into_error_log(start_server):
    """wterm.log는 ERROR 전용이고 backupCount=1이라 이틀치만 남는다. 감사
    기록이 그쪽으로 새면 보존 기간이 사실상 사라지고, 정상 동작이 오류 로그를
    채워 신호 대 잡음도 나빠진다."""
    h = start_server()
    h.login()
    assert "login-ok" in audit_log(h)
    err = h.root / "logs" / "wterm.log"
    contents = err.read_text(encoding="utf-8", errors="replace") if err.exists() else ""
    assert "login-ok" not in contents


def test_audit_marks_the_restart_boundary(start_server):
    """재시작은 토큰과 PTY 세션이 전부 끊기는 경계다. 그 경계가 표시돼야
    "그 시각 이후 기록이 왜 비었는지"를 알 수 있다."""
    h = start_server()
    h.login()
    assert h.stop() == 0
    log = audit_log(h)  # 이 프로세스의 server-start 이후만
    assert "login-ok" in log
    assert "server-stop" in log
    assert log.index("login-ok") < log.index("server-stop")
