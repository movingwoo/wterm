"""인증 — Origin 검증, 로그인/로그아웃, 시도 제한.

여기서 지키는 성질은 AGENTS.md "Authentication"에 적힌 것들이다. 이 서버는
인증이 뚫리면 곧바로 임의 명령 실행이라 회귀가 비싸다.
"""
from __future__ import annotations

import time

import httpx

from conftest import PASSWORD


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


def test_bad_body_is_rejected(start_server):
    h = start_server()
    c = h.client()
    assert c.post("/api/login", content=b"not json").status_code == 400
    assert c.post("/api/login", json={}).status_code == 400


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
