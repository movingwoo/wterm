"""W-Term: Claude Code 웹 원격 제어 서버.

실행: .venv/bin/uvicorn server.main:app --host <host> --port <port>
또는: .venv/bin/python -m server  (projects.json의 host/port 사용)
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .session import SessionManager, has_codex_history, latest_session_id, remote_has_history

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

config = load_config()
manager = SessionManager(grace_seconds=config.grace_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await manager.shutdown()


app = FastAPI(title="W-Term", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── 패스워드 인증 (projects.json에 password_sha256이 있을 때만) ──────────
# 로그인 성공 시 발급한 토큰을 서버 메모리에만 보관한다 (무상태 철학 유지,
# 서버 재시작 시 전부 무효화되어 재로그인 필요).
AUTH_COOKIE = "wterm_token"
AUTH_COOKIE_MAX_AGE = 30 * 24 * 3600
_valid_tokens: set[str] = set()


def is_authed(cookies: dict[str, str]) -> bool:
    if config.password_sha256 is None:
        return True
    token = cookies.get(AUTH_COOKIE)
    return token is not None and token in _valid_tokens


@app.post("/api/login")
async def login(request: Request):
    """패스워드 검증 후 HttpOnly 쿠키로 세션 토큰을 발급한다."""
    if config.password_sha256 is None:
        return JSONResponse({"ok": True})
    try:
        body = await request.json()
        password = str(body["password"])
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        return JSONResponse({"ok": False}, status_code=400)
    digest = hashlib.sha256(password.encode()).hexdigest()
    if not secrets.compare_digest(digest, config.password_sha256):
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
    # log_config=None: uvicorn 자체 로깅 설정을 끄고 위 root 로거로 전파시킴
    if config.uds:
        uds_path = Path(config.uds)
        uds_path.parent.mkdir(parents=True, exist_ok=True)
        if uds_path.exists():
            # 이전 비정상 종료로 남은 소켓 파일. 싱글턴은 run.sh의 PID 파일이
            # 보장하므로 여기서 지워도 안전하다.
            uds_path.unlink()
        uvicorn.run(app, uds=str(uds_path), log_config=None, access_log=False)
    else:
        uvicorn.run(
            app, host=config.host, port=config.port, log_config=None, access_log=False
        )


if __name__ == "__main__":
    main()
