"""보안 응답 헤더와 감사 로그 (TODO 1 티어 3).

둘 다 "있는지 없는지"를 눈으로 확인할 수 없는 종류라 회귀가 조용히 일어난다.
헤더는 라우트가 아니라 미들웨어에 붙어 있어야 /static까지 덮고, 감사 로그는
사후 조사가 유일한 용도라 기록이 빠져도 평상시에는 아무 증상이 없다.
"""
from __future__ import annotations

import time

from test_ws import end_shell, recv_until, send_line, status_message, ws_connect


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
    (AGENTS.md "Session invariants"). 서버 기록이 유일한 단서다."""
    h = start_server()
    token = h.login()
    for path, origin, reason in (
        ("/ws/demo?agent=shell", "https://evil.example", "origin"),
        ("/ws/nope?agent=shell", "", "unknown-project"),
        ("/ws/demo?agent=bogus", "", "bad-agent"),
    ):
        try:
            with ws_connect(h, path, token=token, origin=origin):
                pass
        except Exception:
            pass  # 핸드셰이크가 거절되는 것이 정상
        assert f"ws-reject reason={reason}" in audit_log(h), reason


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
