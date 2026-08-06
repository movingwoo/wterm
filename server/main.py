"""W-Term: Claude Code 웹 원격 제어 서버.

실행: .venv/bin/uvicorn server.main:app --host <host> --port <port>
또는: .venv/bin/python -m server  (projects.json의 host/port 사용)
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import secrets
import signal
import ssl
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .session import SessionManager, has_codex_history, latest_session_id, remote_has_history

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
PID_FILE = BASE_DIR / "logs" / "wterm.pid"

config = load_config()
manager = SessionManager(grace_seconds=config.grace_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await manager.shutdown()


app = FastAPI(title="W-Term", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── 패스워드 인증 (projects.json에 password_hash가 있을 때만) ──────────
# 로그인 성공 시 발급한 토큰을 서버 메모리에만 보관한다 (무상태 철학 유지,
# 서버 재시작 시 전부 무효화되어 재로그인 필요).
AUTH_COOKIE = "wterm_token"
AUTH_COOKIE_MAX_AGE = 30 * 24 * 3600
_valid_tokens: set[str] = set()
_password_hasher = PasswordHasher()


def is_authed(cookies: dict[str, str]) -> bool:
    if config.password_hash is None:
        return True
    token = cookies.get(AUTH_COOKIE)
    return token is not None and token in _valid_tokens


@app.post("/api/login")
async def login(request: Request):
    """패스워드 검증 후 HttpOnly 쿠키로 세션 토큰을 발급한다."""
    if config.password_hash is None:
        return JSONResponse({"ok": True})
    try:
        body = await request.json()
        password = str(body["password"])
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        return JSONResponse({"ok": False}, status_code=400)
    try:
        _password_hasher.verify(config.password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return JSONResponse({"ok": False}, status_code=401)
    token = secrets.token_urlsafe(32)
    _valid_tokens.add(token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        AUTH_COOKIE, token, httponly=True, samesite="lax", max_age=AUTH_COOKIE_MAX_AGE
    )
    return resp


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/projects")
async def list_projects(request: Request):
    """화이트리스트 프로젝트 목록과 각 프로젝트의 상태를 반환한다."""
    if not is_authed(request.cookies):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    result = []
    for p in config.projects:
        result.append(
            {
                "name": p.name,
                "path": p.path,
                "ssh": p.ssh,
                "live": manager.get_live(f"{p.name}#claude") is not None,
                "codex_live": manager.get_live(f"{p.name}#codex") is not None,
                "shell_live": manager.get_live(f"{p.name}#shell") is not None,
                # 원격은 목록 조회 때마다 ssh를 돌리기엔 느려서 낙관적으로 true
                # (실제 판단은 WS 접속 시 remote_has_history로 수행)
                "has_history": True if p.ssh else latest_session_id(p.path) is not None,
                "codex_has_history": True if p.ssh else has_codex_history(p.path),
            }
        )
    return result


@app.websocket("/ws/{project_name}")
async def terminal_ws(
    ws: WebSocket, project_name: str, mode: str = "attach",
    agent: str = "claude", shell: bool = False
):
    """터미널 WebSocket.

    mode:
      - "new":      기존 라이브 세션이 있으면 종료하고 새 claude 세션 시작
      - "resume":   라이브 세션이 있으면 재접속, 없으면 `claude --resume`
                    (터미널 안에서 전체 세션 목록 중 선택)
      - "continue": 라이브 세션이 있으면 재접속, 없으면 `claude -c`
                    (최근 세션 자동 이어하기 — 자동 재연결용)
      - "attach":   라이브 세션이 있으면 재접속, 없으면 새 세션 시작

    shell=1이면 claude 대신 프로젝트 cwd에서 로그인 셸을 띄운다 (ssh 프로젝트면
    원격 셸). 세션 키가 "<name>#shell"로 분리되어 claude 세션과 동시에 유지되고,
    셸에는 이어하기 개념이 없어 mode는 재접속(라이브) 아니면 새 기동으로만 동작한다.

    프로토콜:
      클라이언트→서버: JSON 텍스트 {"type":"input","data":str} | {"type":"resize","cols":N,"rows":M}
      서버→클라이언트: 바이너리(터미널 raw 출력) | JSON 텍스트 {"type":"status"|"exit",...}
    """
    project = config.find_project(project_name)
    if project is None:
        await ws.close(code=4404, reason="화이트리스트에 없는 프로젝트")
        return

    if shell:
        agent = "shell"
    if agent not in ("claude", "codex", "shell"):
        await ws.close(code=4400, reason="지원하지 않는 에이전트")
        return

    await ws.accept()

    # accept 후에 닫아야 close code(4401)가 클라이언트까지 전달된다
    if not is_authed(ws.cookies):
        await ws.close(code=4401, reason="인증 필요")
        return

    session_key = f"{project_name}#{agent}"
    session = manager.get_live(session_key)
    if session is not None and mode != "new":
        await session.attach(ws)
        await ws.send_text(
            json.dumps({"type": "status", "message": "실행 중인 세션에 재접속했습니다."})
        )
    elif agent == "shell":
        session = await manager.start(
            session_key, project.path, ssh=project.ssh, agent="shell"
        )
        await session.attach(ws)
        await ws.send_text(
            json.dumps({"type": "status", "message": "셸 세션을 시작합니다."})
        )
    else:
        if project.ssh is not None:
            has_history = await remote_has_history(project.ssh, project.path, agent)
        else:
            has_history = (
                has_codex_history(project.path)
                if agent == "codex"
                else latest_session_id(project.path) is not None
            )
        if mode == "resume" and has_history:
            extra_args = ["resume"] if agent == "codex" else ["--resume"]
            msg = "이어할 세션을 목록에서 선택하세요."
        elif mode == "continue" and has_history:
            extra_args = ["resume", "--last"] if agent == "codex" else ["--continue"]
            msg = "가장 최근 세션을 이어합니다."
        elif mode in ("resume", "continue"):
            extra_args, msg = None, "이어할 세션 기록이 없어 새 세션을 시작합니다."
        else:
            extra_args, msg = None, "새 세션을 시작합니다."
        session = await manager.start(
            session_key, project.path, extra_args, ssh=project.ssh, agent=agent
        )
        await session.attach(ws)
        await ws.send_text(json.dumps({"type": "status", "message": msg}))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "input":
                session.write_input(msg.get("data", ""))
            elif msg.get("type") == "resize":
                session.resize(int(msg["cols"]), int(msg["rows"]))
    except WebSocketDisconnect:
        pass
    finally:
        session.detach(ws)


# ── TLS (projects.json에 tls_certfile/tls_keyfile이 둘 다 있을 때만) ────
#
# 이 서버는 인증서를 발급하지 않는다. 파일을 읽기만 하고, 갱신은 acme.sh 같은
# 외부 클라이언트가 담당한다(scripts/cert-setup.sh 참고). 갱신 직후 SIGHUP을
# 받으면 같은 SSLContext에 인증서만 다시 물려서 재시작 없이 반영한다 —
# PTY 세션이 서버 프로세스에 붙어 있어 재시작하면 전부 죽기 때문이다.

_tls_log = logging.getLogger("wterm.tls")
# 루트는 ERROR로 잠겨 있지만(setup_logging 참조), 인증서 리로드는 60일에 한 번
# 일어나는 데다 성공 기록이 남아야 점검이 된다. 자식 로거에 자체 레벨을 주면
# 전파 시 상위 "로거의 레벨"은 다시 보지 않고 핸들러만 타므로 INFO가 통과한다.
# 성공을 ERROR로 남기면 로그만 보고 장애로 오해하게 된다.
_tls_log.setLevel(logging.INFO)
_ssl_ctx: ssl.SSLContext | None = None


def _load_cert(ctx: ssl.SSLContext) -> None:
    ctx.load_cert_chain(config.tls_certfile, config.tls_keyfile)


def _on_sighup(signum, frame) -> None:
    """인증서 갱신 알림. 기존 연결은 유지되고 새 연결부터 새 인증서를 쓴다."""
    if _ssl_ctx is None:
        return
    try:
        _load_cert(_ssl_ctx)
        _tls_log.info("인증서를 다시 읽었습니다: %s", config.tls_certfile)
    except Exception as exc:
        # 새 파일이 깨져 있어도 기존 컨텍스트는 온전하므로 서비스는 계속된다.
        _tls_log.error("인증서 리로드 실패, 기존 인증서를 유지합니다: %s", exc)


def _setup_tls():
    """SSLContext를 만들어 보관하고 SIGHUP 훅을 건 뒤, uvicorn에 넘길 팩토리를 반환."""
    global _ssl_ctx
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    _load_cert(ctx)  # 기동 시점의 실패는 감추지 않고 그대로 터뜨린다
    _ssl_ctx = ctx
    # uvicorn이 가로채는 시그널은 SIGINT/SIGTERM뿐이라 SIGHUP 핸들러는 살아남는다.
    signal.signal(signal.SIGHUP, _on_sighup)
    return lambda cfg, default_factory: ctx


# ── PID 파일 ────────────────────────────────────────────────────────
#
# 서버가 직접 쓴다. start.sh(백그라운드 기동)와 launchd(포그라운드 기동) 어느
# 쪽으로 띄워도 같은 파일이 나와야 stop.sh와 인증서 --reloadcmd가 동작한다.


def _claim_pid_file() -> None:
    PID_FILE.parent.mkdir(exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_release_pid_file)


def _release_pid_file() -> None:
    # 내가 쓴 pid일 때만 지운다. 늦게 죽는 이전 프로세스가 새 서버의 pid 파일을
    # 지워버리는 상황을 막기 위한 것.
    try:
        if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def setup_logging() -> None:
    """ERROR 이상만 기록. 자정마다 로테이션하며 하루 치(직전 파일 1개)만 보관."""
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = TimedRotatingFileHandler(
        log_dir / "wterm.log", when="midnight", backupCount=1, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.ERROR)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def main() -> None:
    import uvicorn

    setup_logging()
    _claim_pid_file()
    # log_config=None: uvicorn 자체 로깅 설정을 끄고 위 root 로거로 전파시킴
    kwargs = {"log_config": None, "access_log": False}
    if config.uds:
        if config.tls_enabled:
            # UDS는 앞단 프록시가 TLS를 담당하는 구성이므로 여기서 겹칠 이유가 없다.
            _tls_log.error("uds 사용 중이라 tls_certfile/tls_keyfile을 무시합니다")
        uds_path = Path(config.uds)
        uds_path.parent.mkdir(parents=True, exist_ok=True)
        if uds_path.exists():
            # 이전 비정상 종료로 남은 소켓 파일. 싱글턴은 run.sh의 PID 파일이
            # 보장하므로 여기서 지워도 안전하다.
            uds_path.unlink()
        uvicorn.run(app, uds=str(uds_path), **kwargs)
    else:
        if config.tls_enabled:
            kwargs["ssl_context_factory"] = _setup_tls()
        uvicorn.run(app, host=config.host, port=config.port, **kwargs)


if __name__ == "__main__":
    main()
