"""인증 — Origin 검증, 로그인/로그아웃, 시도 제한.

여기서 지키는 성질은 AGENTS.md "Authentication"에 적힌 것들이다. 이 서버는
인증이 뚫리면 곧바로 임의 명령 실행이라 회귀가 비싸다.
"""
from __future__ import annotations

import time

import httpx
import pytest
from websockets.exceptions import ConnectionClosed

from conftest import PASSWORD, free_port, wait_for_uds
from test_ws import RECV_TIMEOUT, end_shell, recv_until, send_line, status_message, ws_connect


def test_index_is_served(start_server):
    h = start_server()
    r = h.client().get("/")
    assert r.status_code == 200
    assert "xterm" in r.text


def test_projects_requires_auth(start_server):
    h = start_server()
    assert h.client().get("/api/projects").status_code == 401


def test_login_flow_and_project_list(start_server):
    h = start_server()
    c = h.client()

    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert not c.cookies.get("wterm_token")

    r = c.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json() == {"ok": True}

    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    # 평문 HTTP로 띄운 서버이므로 secure가 붙으면 브라우저가 쿠키를 버린다.
    assert "secure" not in set_cookie

    projects = c.get("/api/projects")
    assert projects.status_code == 200
    (demo,) = projects.json()
    assert demo["name"] == "demo"
    assert demo["ssh"] is None
    assert (demo["live"], demo["codex_live"], demo["shell_live"]) == (False, False, False)


def test_logout_revokes_token_server_side(start_server):
    """쿠키를 지우는 것만으로는 부족하다 — 탈취된 토큰이 그대로 살아 있으면 안 된다."""
    h = start_server()
    c = h.client()
    c.post("/api/login", json={"password": PASSWORD})
    token = c.cookies["wterm_token"]
    assert c.get("/api/projects").status_code == 200

    assert c.post("/api/logout").status_code == 200

    # 클라이언트가 쿠키를 버렸는지가 아니라, 서버가 토큰을 폐기했는지를 본다.
    stale = h.client(headers={"Origin": h.origin, "Cookie": f"wterm_token={token}"})
    assert stale.get("/api/projects").status_code == 401


def test_logout_closes_live_websocket(start_server):
    """토큰만 지우는 것으로는 폐기가 되지 않는다.

    인증 검사는 핸드셰이크 때 한 번뿐이라, 이미 붙어 있는 소켓은 로그아웃 뒤에도
    그대로 살아서 명령을 계속 실행한다 — 폰을 잃어버렸을 때 "다른 기기에서
    로그아웃"이 아무것도 못 끊는다는 뜻이다. 4401로 닫아야 app.js가 로그인
    화면을 띄운다.
    """
    h = start_server()
    c = h.client()
    c.post("/api/login", json={"password": PASSWORD})
    token = c.cookies["wterm_token"]

    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        send_line(ws, "echo BEFORE-LOGOUT")
        recv_until(ws, "BEFORE-LOGOUT")

        assert c.post("/api/logout").status_code == 200

        with pytest.raises(ConnectionClosed) as exc:
            while True:
                ws.recv(timeout=RECV_TIMEOUT)
        assert exc.value.rcvd.code == 4401

    # 폐기된 토큰으로는 다시 붙지도 못한다 (여기가 실제로 막고 싶은 것).
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=RECV_TIMEOUT)
    assert exc.value.rcvd.code == 4401

    # 세션 자체는 유예 시간 동안 살아 있다. 다시 로그인하면 그대로 이어붙는다
    # (로그아웃은 접근 폐기이지 실행 중인 프로세스를 죽이는 수단이 아니다).
    with ws_connect(h, "/ws/demo?agent=shell", token=h.login()) as ws:
        assert "재접속" in status_message(ws)
        recv_until(ws, "BEFORE-LOGOUT")  # 같은 프로세스의 replay 버퍼
        end_shell(ws)


def test_token_limit_wakes_watchdog_and_closes_live_websocket(start_server):
    """오래된 토큰이 상한에 밀리면 열린 소켓도 즉시 접근을 잃는다.

    30일 TTL을 기다릴 수는 없지만 watchdog이 보는 조건은 둘 다 같은
    ``_key_valid`` 결과다. 싼 테스트용 argon2 해시로 MAX_TOKENS(512) 경계를
    실제 로그인 요청으로 넘기면, 저장소 변경 알림에 깨어난 watchdog과 4401
    프론트엔드 계약을 시간 축소용 테스트 설정 없이 함께 검증할 수 있다.
    """
    h = start_server()
    c = h.client()
    first = c.post("/api/login", json={"password": PASSWORD})
    assert first.status_code == 200
    oldest = first.cookies["wterm_token"]

    with ws_connect(h, "/ws/demo?agent=shell", token=oldest) as ws:
        status_message(ws)

        # 첫 토큰을 포함해 513개가 되면 가장 오래된 first가 축출된다. 성공한
        # 로그인은 시도 제한에 걸리지 않고 conftest의 저비용 해시를 검증한다.
        latest = None
        for _ in range(512):
            issued = c.post("/api/login", json={"password": PASSWORD})
            assert issued.status_code == 200
            latest = issued.cookies["wterm_token"]

        with pytest.raises(ConnectionClosed) as exc:
            while True:
                ws.recv(timeout=RECV_TIMEOUT)
        assert exc.value.rcvd.code == 4401

    # 축출된 키는 새 요청에도 거부되고, 가장 최근 토큰은 그대로 유효하다.
    stale = h.client(headers={"Origin": h.origin, "Cookie": f"wterm_token={oldest}"})
    assert stale.get("/api/projects").status_code == 401
    assert latest is not None
    fresh = h.client(headers={"Origin": h.origin, "Cookie": f"wterm_token={latest}"})
    assert fresh.get("/api/projects").status_code == 200

    # 인증 폐기는 프로세스를 죽이지 않는다. 새 토큰으로 같은 PTY에 붙어 끝낸다.
    with ws_connect(h, "/ws/demo?agent=shell", token=latest) as ws:
        assert "재접속" in status_message(ws)
        end_shell(ws)


def test_post_requires_matching_origin(start_server):
    """WS/POST는 CORS가 막아주지 않으므로 서버가 직접 Origin을 본다."""
    h = start_server()
    with httpx.Client(base_url=h.base_url, timeout=10.0) as c:
        # 다른 사이트에서 온 요청 — 패스워드가 맞아도 통과하면 안 된다.
        r = c.post(
            "/api/login",
            json={"password": PASSWORD},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403
        assert "set-cookie" not in r.headers

        # Origin이 아예 없는 요청은 브라우저가 보낸 것이 아니다.
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 403


def test_allowed_origins_overrides_host_rule(start_server):
    """프록시가 Host를 바꿔 쓰는 구성용 탈출구."""
    h = start_server(allowed_origins=["https://wterm.example.com:8443"])
    with httpx.Client(base_url=h.base_url, timeout=10.0) as c:
        ok = c.post(
            "/api/login",
            json={"password": PASSWORD},
            headers={"Origin": "https://wterm.example.com:8443"},
        )
        assert ok.status_code == 200
        # 화이트리스트가 있으면 Origin==Host 규칙은 더 이상 통하지 않는다.
        assert c.post(
            "/api/login", json={"password": PASSWORD}, headers={"Origin": h.origin}
        ).status_code == 403


def test_uds_cookie_stays_secure_unless_proxy_says_plaintext(
    start_server, short_tmp_dir
):
    """uds 뒤에서는 uvicorn이 scheme을 보정해 주지 못한다.

    유닉스 소켓에는 클라이언트 주소가 없어(scope["client"]가 None)
    forwarded_allow_ips 판정이 **항상** 실패하고, X-Forwarded-Proto가 무시된 채
    scheme이 "http"로 남는다. 그것만 믿으면 HTTPS로 서비스하는 구성에서 토큰
    쿠키가 Secure 없이 나간다 — uds는 앞단이 TLS를 맡는 구성이라는 것이 이
    서버의 전제이므로 그쪽을 기본값으로 두고, 평문 프록시는 헤더로 뒤집는다.
    """
    sock = short_tmp_dir / "w.sock"
    h = start_server(wait=False, uds=str(sock), port=free_port())
    wait_for_uds(h, sock)

    with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), timeout=10.0) as c:
        r = c.post(
            "http://wterm.local/api/login",
            json={"password": PASSWORD},
            headers={"Origin": "https://wterm.local"},
        )
        assert r.status_code == 200, r.text
        assert "secure" in r.headers["set-cookie"].lower()

        # 같은 이유로 평문 오리진도 이 구성에서는 통과하면 안 된다.
        assert c.post(
            "http://wterm.local/api/login",
            json={"password": PASSWORD},
            headers={"Origin": "http://wterm.local"},
        ).status_code == 403

        # 앞단이 평문이라고 밝히면 Secure를 빼야 브라우저가 쿠키를 받는다.
        plain = c.post(
            "http://wterm.local/api/login",
            json={"password": PASSWORD},
            headers={"Origin": "http://wterm.local", "X-Forwarded-Proto": "http"},
        )
        assert plain.status_code == 200, plain.text
        assert "secure" not in plain.headers["set-cookie"].lower()


def test_bad_body_is_rejected(start_server):
    h = start_server()
    c = h.client()
    assert c.post("/api/login", content=b"not json").status_code == 400
    assert c.post("/api/login", json={}).status_code == 400


def test_login_body_size_is_capped(start_server):
    """본문 상한이 없으면 argon2 비용 방어가 그 앞단의 메모리로 우회된다.

    /api/login은 인증 이전 경로이고 시도 제한은 LOGIN_FREE_ATTEMPTS회를 지나야
    걸리므로, 그 사이에 request.json()이 몇 백 MB짜리 본문을 통째로 메모리에
    올릴 수 있다. 받는 것이 패스워드 하나뿐이라 상한을 낮게 잡아도 잃는 것이 없다.
    """
    h = start_server()
    c = h.client()

    assert c.post("/api/login", json={"password": "x" * 20000}).status_code == 413

    # 길이를 안 밝히는 것만으로 검사를 건너뛸 수 있으면 상한이 무의미하다.
    def chunked():
        yield b'{"password": "whatever"}'

    assert c.post("/api/login", content=chunked()).status_code == 411

    # 상한에 걸린 요청은 argon2를 태우지 않으므로 시도 횟수에도 잡히지 않는다.
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200


def test_login_rate_limit(start_server):
    """차단은 argon2 비용을 태우지 않고 먼저 끊는 것이 요점이다."""
    h = start_server()
    c = h.client()

    # LOGIN_FREE_ATTEMPTS(5)까지는 지연 없이 401.
    for _ in range(5):
        assert c.post("/api/login", json={"password": "wrong"}).status_code == 401

    # 6번째 실패가 차단을 건다 (2^(6-5) = 2초).
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401

    blocked = c.post("/api/login", json={"password": PASSWORD})
    assert blocked.status_code == 429, "차단 중에는 올바른 패스워드도 거부해야 한다"
    assert int(blocked.headers["Retry-After"]) >= 1
    assert "set-cookie" not in blocked.headers

    # 차단이 풀리면 성공하고, 성공은 실패 카운트를 지운다.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        r = c.post("/api/login", json={"password": PASSWORD})
        if r.status_code == 200:
            break
        assert r.status_code == 429
        time.sleep(0.25)
    else:
        raise AssertionError("차단이 풀리지 않았다")
    assert c.get("/api/projects").status_code == 200


def test_auth_disabled_without_password_hash(start_server):
    """password_hash가 없으면 인증 자체가 꺼진다 (Origin 검증은 그대로)."""
    h = start_server(password_hash=None)
    c = h.client()
    assert c.get("/api/projects").status_code == 200
    assert c.post("/api/login", json={}).status_code == 200
