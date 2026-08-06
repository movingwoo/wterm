"""자체 TLS 종단과 SIGHUP 인증서 리로드.

AGENTS.md "TLS"에 적힌 검증 절차를 그대로 자동화한 것이다: HTTPS가 응답하고,
wss 업그레이드가 되고, 파일을 덮어써도 SIGHUP 전에는 바뀌지 않고, SIGHUP이
프로세스를 죽이지 않고 인증서만 교체하며, 깨진 인증서는 기존 것을 유지한다.

재시작으로 인증서를 반영하면 안 되는 이유가 이 테스트의 핵심이다 — PTY 세션이
서버 프로세스에 붙어 있어서 재시작은 곧 전 세션 종료다.
"""
from __future__ import annotations

import json
import shutil
import signal
import ssl
import subprocess
import time

import httpx
import pytest
from websockets.sync.client import connect

from conftest import PASSWORD, free_port
from test_ws import end_shell

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl이 없어 테스트 인증서를 만들 수 없다"
)


def make_cert(tmp_path, name: str):
    """자체 서명 인증서 한 쌍. 이름이 다르면 인증서도 다르다."""
    cert = tmp_path / f"{name}.pem"
    key = tmp_path / f"{name}.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
            "-days", "1", "-nodes",
            "-subj", f"/CN={name}.wterm.test",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True, capture_output=True, timeout=120,
    )
    return cert, key


def served_cert(h) -> str:
    """지금 서버가 내주는 인증서(PEM). 파일이 아니라 실제로 협상된 것을 본다."""
    return _normalize(ssl.get_server_certificate((h.host, h.port), timeout=10))


def _normalize(pem: str) -> str:
    return "".join(pem.split())


def wait_for_cert(h, expected: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if served_cert(h) == expected:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def tls_server(start_server, tmp_path):
    """인증서 A로 뜬 HTTPS 서버와, 나중에 갈아끼울 인증서 B."""
    cert_a, key_a = make_cert(tmp_path, "alpha")
    cert_b, key_b = make_cert(tmp_path, "bravo")
    # 서버가 읽는 경로는 고정해 두고 내용만 바꾼다 (acme.sh의 갱신 방식과 같다).
    live_cert = tmp_path / "fullchain.pem"
    live_key = tmp_path / "privkey.pem"
    shutil.copy(cert_a, live_cert)
    shutil.copy(key_a, live_key)
    h = start_server(tls_certfile=str(live_cert), tls_keyfile=str(live_key))
    return h, (live_cert, live_key), (cert_a, key_a), (cert_b, key_b)


def test_https_and_wss_work(tls_server):
    h, _, (cert_a, _), _ = tls_server
    assert served_cert(h) == _normalize(cert_a.read_text())

    with httpx.Client(verify=False, timeout=10.0) as c:
        r = c.post(
            f"{h.base_url}/api/login",
            json={"password": PASSWORD},
            headers={"Origin": h.origin},
        )
        assert r.status_code == 200
        # TLS로 들어온 요청이므로 쿠키에 secure가 붙어야 한다.
        assert "secure" in r.headers["set-cookie"].lower()
        token = r.cookies["wterm_token"]

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with connect(
        f"{h.ws_url}/ws/demo?agent=shell",
        ssl=ctx,
        additional_headers={"Origin": h.origin, "Cookie": f"wterm_token={token}"},
        open_timeout=15,
    ) as ws:
        msg = ws.recv(timeout=20)
        while isinstance(msg, bytes):
            msg = ws.recv(timeout=20)
        assert json.loads(msg)["type"] == "status"
        end_shell(ws)


def test_sighup_reloads_without_restarting(tls_server):
    h, (live_cert, live_key), _, (cert_b, key_b) = tls_server
    pid_before = h.pid_file.read_text()

    live_cert.write_text(cert_b.read_text())
    live_key.write_text(key_b.read_text())
    # 파일만 바뀐 상태에서는 아직 옛 인증서를 내준다.
    assert served_cert(h) != _normalize(cert_b.read_text())

    h.proc.send_signal(signal.SIGHUP)
    assert wait_for_cert(h, _normalize(cert_b.read_text())), "SIGHUP 후에도 교체되지 않았다"

    # 프로세스는 그대로다 — 재시작이면 PTY 세션이 전부 죽는다.
    assert h.proc.poll() is None
    assert h.pid_file.read_text() == pid_before
    assert "인증서를 다시 읽었습니다" in h.output()

    with httpx.Client(verify=False, timeout=10.0) as c:
        assert c.get(f"{h.base_url}/").status_code == 200


def test_broken_cert_keeps_previous_one(tls_server):
    h, (live_cert, live_key), _, (cert_b, key_b) = tls_server
    live_cert.write_text(cert_b.read_text())
    live_key.write_text(key_b.read_text())
    h.proc.send_signal(signal.SIGHUP)
    assert wait_for_cert(h, _normalize(cert_b.read_text()))

    live_cert.write_text("-----BEGIN CERTIFICATE-----\n깨진 파일\n")
    h.proc.send_signal(signal.SIGHUP)
    time.sleep(1.0)  # 실패한 리로드가 반영될 시간을 준 뒤에 확인한다

    assert h.proc.poll() is None, "리로드 실패로 서버가 죽으면 안 된다"
    assert served_cert(h) == _normalize(cert_b.read_text())
    assert "인증서 리로드 실패" in h.output()


def test_missing_cert_at_startup_is_fatal(start_server, tmp_path):
    """기동 시점의 실패는 감추지 않는다 — 조용히 평문으로 뜨면 더 나쁘다."""
    h = start_server(
        wait=False,
        tls_certfile=str(tmp_path / "없는파일.pem"),
        tls_keyfile=str(tmp_path / "없는파일.key"),
    )
    assert h.proc.wait(timeout=30) != 0
    # pid 파일까지 정리돼야 다음 기동이 잠금에 걸리지 않는다.
    assert not h.pid_file.exists()


def test_tls_ignored_under_uds(start_server, tmp_path, short_tmp_dir):
    """uds 구성에서는 앞단 프록시가 TLS를 담당한다. 서버는 무시하고 뜬다."""
    cert, key = make_cert(tmp_path, "unused")
    # 유닉스 소켓 경로에는 길이 제한(macOS 104바이트)이 있다. pytest의 tmp_path는
    # 그보다 길어서 여기서만 짧은 경로를 쓴다.
    sock = short_tmp_dir / "w.sock"
    h = start_server(
        wait=False,
        uds=str(sock),
        port=free_port(),
        tls_certfile=str(cert),
        tls_keyfile=str(key),
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if sock.exists() and h.pid_file.exists():
            break
        assert h.proc.poll() is None, f"기동 중 종료됨:\n{h.output()}"
        time.sleep(0.1)
    else:
        raise AssertionError(f"유닉스 소켓이 생기지 않았다:\n{h.output()}")

    assert "무시합니다" in h.output()
    transport = httpx.HTTPTransport(uds=str(sock))
    with httpx.Client(transport=transport, timeout=10.0) as c:
        assert c.get("http://wterm.local/").status_code == 200
