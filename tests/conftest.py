"""스모크 테스트 공통 픽스처.

테스트는 **저장소 사본**에서 서버를 실제 프로세스로 띄운다. in-process로 앱을
import하지 않는 이유는 두 가지다:

1. `server/main.py`는 import 시점에 저장소 루트의 `projects.json`을 읽고,
   `logs/wterm.pid`를 실제 경로로 잡는다. 개발 머신에서 테스트를 돌릴 때
   운영 설정을 건드리거나 실행 중인 서버의 pid 파일과 충돌하면 안 된다.
2. pid 잠금, SIGTERM→종료코드 0, SIGHUP 인증서 리로드처럼 이 프로젝트에서
   실제로 깨졌던 것들은 전부 프로세스 수준 동작이라 진짜로 띄워야 검증된다.

사본은 세션당 한 번 만들고, 테스트마다 `projects.json`을 새로 쓴 뒤 서버를
띄운다. 사본 하나를 공유하므로 서버는 한 번에 하나씩만 뜬다(pid 잠금 테스트는
이 성질을 그대로 이용한다).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PASSWORD = "smoke-test-password"

# argon2 기본 파라미터는 검증 1회에 64 MiB / ~35ms를 쓴다. 검증 로직 자체를
# 보는 테스트라 비용까지 재현할 이유가 없어 싼 파라미터로 해시를 만든다
# (파라미터는 해시 문자열에 실려 있어 서버의 기본 PasswordHasher가 그대로 검증한다).
def _cheap_hash(password: str) -> str:
    from argon2 import PasswordHasher

    return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(password)


PASSWORD_HASH = _cheap_hash(PASSWORD)

# 서버 사본에 넣지 않을 것들. projects.json(운영 설정)이 절대 따라가면 안 된다.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", ".claude", ".codex", "logs", "run",
    "__pycache__", "*.pyc", ".pytest_cache", "projects*.json", "tests", ".github",
)

STARTUP_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 20.0


def free_port() -> int:
    """지금 비어 있는 TCP 포트. 바인딩까지의 경합은 있지만 로컬 테스트에선 충분하다."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ServerHandle:
    root: Path
    proc: subprocess.Popen
    host: str
    port: int
    out_path: Path
    tls: bool = False
    _clients: list[httpx.Client] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"{'https' if self.tls else 'http'}://{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        return self.base_url

    @property
    def pid_file(self) -> Path:
        return self.root / "logs" / "wterm.pid"

    @property
    def ws_url(self) -> str:
        return f"{'wss' if self.tls else 'ws'}://{self.host}:{self.port}"

    def client(self, **kwargs) -> httpx.Client:
        """이 서버를 가리키는 httpx 클라이언트. Origin은 기본으로 붙여 둔다."""
        kwargs.setdefault("base_url", self.base_url)
        kwargs.setdefault("headers", {"Origin": self.origin})
        kwargs.setdefault("timeout", 10.0)
        if self.tls:
            kwargs.setdefault("verify", False)
        c = httpx.Client(**kwargs)
        self._clients.append(c)
        return c

    def login(self, password: str = PASSWORD) -> str:
        """로그인해서 세션 토큰을 얻는다."""
        with httpx.Client(verify=False, timeout=10.0) as c:
            r = c.post(
                f"{self.base_url}/api/login",
                json={"password": password},
                headers={"Origin": self.origin},
            )
        assert r.status_code == 200, r.text
        return r.cookies["wterm_token"]

    def output(self) -> str:
        out = self.out_path
        return out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""

    def stop(self, sig: int = signal.SIGTERM) -> int | None:
        """시그널을 보내고 종료 코드를 돌려준다. 이미 죽었으면 그대로 반환."""
        for c in self._clients:
            c.close()
        self._clients.clear()
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            return self.proc.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
            raise AssertionError(f"{SHUTDOWN_TIMEOUT}초 안에 종료되지 않음")


@pytest.fixture(scope="session")
def repo_copy(tmp_path_factory) -> Path:
    """서버를 띄울 저장소 사본. 운영 `projects.json`과 `logs/`가 섞이지 않게 한다."""
    dest = tmp_path_factory.mktemp("wterm") / "repo"
    shutil.copytree(REPO_ROOT, dest, ignore=_COPY_IGNORE)
    (dest / "logs").mkdir(exist_ok=True)
    return dest


@pytest.fixture
def short_tmp_dir():
    """유닉스 소켓용 짧은 임시 디렉터리.

    소켓 경로에는 길이 제한(macOS 104바이트, 리눅스 108바이트)이 있는데
    pytest의 tmp_path는 macOS에서 그것만으로 이미 한계에 가깝다.
    """
    d = Path(tempfile.mkdtemp(prefix="wt", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def project_dir(repo_copy) -> Path:
    """화이트리스트에 넣을 프로젝트 디렉터리 (load_config가 존재를 확인한다)."""
    d = repo_copy / "demo-project"
    d.mkdir(exist_ok=True)
    return d


@pytest.fixture
def start_server(repo_copy, project_dir):
    """서버를 띄우는 팩토리. 테스트가 끝나면 전부 내린다.

    `**overrides`는 그대로 projects.json에 반영된다. `env`로 서버 프로세스의
    환경변수를 덮어쓸 수 있고(PATH에서 claude를 지우는 등), 값이 None이면 그
    변수를 아예 지운다 — $SHELL이 없는 데몬 환경을 재현할 때 쓴다.
    """
    started: list[ServerHandle] = []

    def _start(
        *, env: dict[str, str | None] | None = None, wait: bool = True, **overrides
    ) -> ServerHandle:
        cfg = {
            "host": "127.0.0.1",
            "port": free_port(),
            # 재접속 replay를 확인할 만큼은 길게. 서버 종료 시 어차피 정리된다.
            "grace_seconds": 30,
            "password_hash": PASSWORD_HASH,
            "projects": [{"name": "demo", "path": str(project_dir)}],
        }
        cfg.update(overrides)
        (repo_copy / "projects.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )
        handle = _spawn(repo_copy, cfg, env)
        started.append(handle)
        if wait:
            _wait_ready(handle)
        return handle

    yield _start

    for h in reversed(started):
        try:
            h.stop()
        except Exception:  # 개별 정리 실패가 다른 서버 정리를 막지 않도록
            pass


def _spawn(
    root: Path, cfg: dict, env_overrides: dict[str, str | None] | None
) -> ServerHandle:
    env = dict(os.environ)
    # 데몬은 로그인 셸을 거치지 않지만, 테스트에서는 셸 세션이 어떤 셸을 띄우는지
    # 고정하는 편이 낫다 (login_shell()은 $SHELL을 먼저 본다).
    env["SHELL"] = "/bin/bash"
    env["PYTHONUNBUFFERED"] = "1"
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    (root / "logs").mkdir(exist_ok=True)
    # 서버마다 다른 파일에 남긴다. 하나로 모으면 두 번째 기동(잠금 거부 테스트)이
    # 첫 번째 서버의 출력을 지워버린다.
    out_path = root / "logs" / f"wterm-{cfg['port']}.out"
    out = out_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server"],
        cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    out.close()
    return ServerHandle(
        root=root,
        proc=proc,
        host=cfg["host"],
        port=cfg["port"],
        out_path=out_path,
        tls=bool(cfg.get("tls_certfile") and cfg.get("tls_keyfile")),
    )


def wait_for_uds(h: ServerHandle, sock: Path, timeout: float = STARTUP_TIMEOUT) -> None:
    """유닉스 소켓으로 뜬 서버가 준비될 때까지 기다린다.

    `_wait_ready`는 host/port로 HTTP를 찔러 보므로 uds 구성에서는 쓸 수 없다
    (`start_server(wait=False, uds=...)`와 짝을 이룬다).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc = h.proc.poll()
        if rc is not None:
            raise AssertionError(f"서버가 기동 중 종료됨 (exit {rc})\n{h.output()}")
        if sock.exists() and h.pid_file.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{timeout}초 안에 유닉스 소켓이 생기지 않았다:\n{h.output()}")


def _wait_ready(h: ServerHandle) -> None:
    """pid 파일과 HTTP 응답이 모두 확인될 때까지 기다린다."""
    deadline = time.monotonic() + STARTUP_TIMEOUT
    with httpx.Client(verify=False, timeout=2.0) as c:
        while time.monotonic() < deadline:
            rc = h.proc.poll()
            if rc is not None:
                raise AssertionError(
                    f"서버가 기동 중 종료됨 (exit {rc})\n--- wterm.out ---\n{h.output()}"
                )
            try:
                if c.get(f"{h.base_url}/").status_code == 200 and h.pid_file.exists():
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    raise AssertionError(
        f"{STARTUP_TIMEOUT}초 안에 기동 확인 실패\n--- wterm.out ---\n{h.output()}"
    )
