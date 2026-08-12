"""W-Term: Claude Code 웹 원격 제어 서버.

실행: .venv/bin/uvicorn server.main:app --host <host> --port <port>
또는: .venv/bin/python -m server  (projects.json의 host/port 사용)
"""
from __future__ import annotations

import asyncio
import atexit
import errno
import fcntl
import json
import logging
import os
import signal
import ssl
import time
from contextlib import asynccontextmanager, suppress
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .audit import (
    audit as _audit,
    audit_throttled as _audit_throttled,
    peer as _peer,
    setup_audit_logging,
)
from .auth import AuthManager
from .config import CONFIG_PATH, load_config
from .project_status import ProjectStatusHub
from .session import (
    HISTORY_CHECK_CLOSE_CODE,
    HistoryState,
    SessionManager,
    has_codex_history,
    latest_session_id,
    remote_has_history,
)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
PID_FILE = BASE_DIR / "logs" / "wterm.pid"

config = load_config()
auth = AuthManager(config.password_hash)
manager = SessionManager(
    grace_seconds=config.grace_seconds,
    idle_seconds=config.idle_seconds,
    # 콜백은 세션을 시작할 때 처음 실행되므로 아래 hub 할당이 끝난 뒤다.
    state_changed=lambda: project_status.changed(),
)
project_status = ProjectStatusHub(config, manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    status_task = asyncio.create_task(project_status.run())
    try:
        yield
    finally:
        status_task.cancel()
        with suppress(asyncio.CancelledError):
            await status_task
        await manager.shutdown()


app = FastAPI(title="W-Term", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── 보안 응답 헤더 ──────────────────────────────────────────────────
#
# 라우트 데코레이터가 아니라 미들웨어인 이유는 /static이 StaticFiles 마운트라
# 라우트 훅을 타지 않기 때문이다. app.js가 헤더 없이 나가면 CSP는 의미가 없다.
#
# script-src가 이 목록의 핵심이다. 이 서버에서 스크립트 주입은 곧 임의 명령
# 실행이라, 외부 스크립트와 인라인 스크립트를 둘 다 막는 것이 실질적인 방어다.
# index.html에는 인라인 스크립트도 이벤트 속성도 없어 'self'로 그냥 통과한다.
#
# style-src만 'unsafe-inline'을 연다. xterm.js DOM 렌더러가 런타임에 <style>
# 엘리먼트를 만들어 textContent로 CSS를 밀어넣기 때문이다(_dimensionsStyleElement,
# _themeStyleElement). 벤더링과 무관하게 이건 인라인 스타일로 취급되어 차단되고,
# 차단되면 셀 크기와 테마 색이 어긋난 채로 렌더링된다. nonce로도 못 푼다 —
# 엘리먼트를 만드는 것이 우리 코드가 아니라 xterm.js라 nonce를 붙일 수 없다.
# 스타일 주입만으로는 명령 실행에 이르지 못하므로 여기서 멈춘다.
#
# HSTS는 넣지 않는다. tls_enabled면 평문 리스너 자체가 없어 강등당할 대상이
# 없고, 남는 위협(그 호스트명을 사칭하는 중간자)은 이미 테일넷 안에 들어와 있어야
# 성립한다. 얻는 것에 비해 브라우저에 오래 남는 부작용이 크다.
SECURITY_HEADERS = {
    "Content-Security-Policy": "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",  # xterm.js 런타임 <style> 주입
            "img-src 'self'",
            "font-src 'self'",
            "connect-src 'self'",  # CSP3에서 'self'는 같은 호스트의 wss도 포함한다
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",  # 터미널 클릭재킹
            "object-src 'none'",
        )
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # frame-ancestors를 이해하지 못하는 구형 브라우저용 중복 방어
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    # 이 앱은 카메라도 위치도 쓰지 않는다. 얻는 것은 크지 않지만, 스크립트 주입이
    # 곧 임의 명령 실행인 서버에서 주입된 코드가 쓸 수 있는 것을 줄여 둔다.
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "microphone=(), payment=(), usb=()"
    ),
    # 다른 오리진이 이 서버의 응답을 <script>/<img>로 끌어가 그 내용을 재는 것
    # (Spectre류 사이드채널)을 막는다. 우리 페이지는 전부 같은 오리진에서 받는다.
    "Cross-Origin-Resource-Policy": "same-origin",
}

# /api 응답은 캐시하지 않는다. /api/projects에는 프로젝트 이름과 **로컬 경로**가
# 실리고 /api/login은 토큰 쿠키를 발급하는 자리라, 공유 기기의 브라우저 캐시나
# 중간 캐시에 남을 이유가 없다.
NO_STORE_PREFIX = "/api/"

# 정적 자원은 캐시하되 **매번 확인하고** 쓴다.
#
# StaticFiles는 Cache-Control을 붙이지 않는데, 그 헤더가 없으면 브라우저는 캐시를
# 버리는 게 아니라 반대로 휴리스틱 캐싱을 한다 — 마지막 수정 이후 경과 시간의 10%
# 동안 서버에 묻지도 않고 사본을 쓴다. 며칠 묵은 app.js를 받아둔 탭은 그만큼
# 오래 옛 코드를 돌린다. 프론트를 고쳐도 반영이 안 되고, 하필 이 앱은 탭을 계속
# 열어두는 물건이라 그 창이 제일 오래 산다.
#
# no-store가 아니라 no-cache인 것은 "캐시하지 마라"가 아니라 "쓰기 전에 물어봐라"가
# 필요해서다. 조건부 요청이 ETag로 304를 받으면 본문은 오지 않으므로, 290KB짜리
# xterm.js 사본을 매번 다시 받는 일도 없다.
#
# "/"도 같이 본다. index.html은 StaticFiles가 아니라 FileResponse로 나가지만
# Cache-Control이 없기는 마찬가지고, 그것이 나머지를 부르는 문서다.
NO_CACHE_PREFIX = "/static/"


def _no_cache_path(path: str) -> bool:
    return path == "/" or path.startswith(NO_CACHE_PREFIX)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith(NO_STORE_PREFIX):
        response.headers.setdefault("Cache-Control", "no-store")
    elif _no_cache_path(request.url.path):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

# ── Origin 검증 ─────────────────────────────────────────────────────
#
# WebSocket 핸드셰이크에는 CORS가 적용되지 않는다. 쿠키만 확인하면, 로그인해 둔
# 사용자가 아무 웹페이지나 방문했을 때 그 페이지가 wss://<이 서버>/ws/... 를 열 수
# 있고 브라우저가 쿠키를 붙여준다(CSWSH). 이 서버에서 그것은 곧 임의 명령 실행이라
# SameSite 쿠키 하나에 기대지 않고 Origin을 직접 확인한다.
#
# 브라우저는 WS 핸드셰이크와 POST에 Origin을 반드시 실어 보내고, 공격자는 피해자의
# 브라우저가 이 서버로 보내는 Host를 바꿀 수 없다. 그래서 기본 규칙은 "Origin의
# 호스트 == Host 헤더"로 충분하다. 프록시가 Host를 바꿔 쓰는 구성이라면
# projects.json의 allowed_origins로 명시한다.
#
# Origin이 아예 없는 요청은 브라우저가 보낸 것이 아니므로 거부한다. 이 서버의
# 클라이언트는 static/app.js뿐이라 잃는 것이 없다.


def _is_secure(conn) -> bool:
    """이 요청이 https/wss로 들어왔는지. Request와 WebSocket 둘 다 받는다.
    쿠키의 Secure 플래그와 아래 Origin 스킴 검사가 같은 판정을 쓴다.

    - 서버가 직접 TLS를 종단하면 평문으로 들어올 길 자체가 없다.
    - uds 구성은 앞단 프록시가 TLS를 담당하는 형태다. 그런데 유닉스 소켓에는
      클라이언트 주소가 없어(scope["client"]가 None — 확인함) uvicorn의
      forwarded_allow_ips 판정이 **항상** 실패하고, X-Forwarded-Proto가 무시된
      채 scheme이 "http"로 남는다. HTTPS로 서비스하면서 토큰 쿠키가 Secure 없이
      나가는 상황이라, 이 구성에서는 헤더를 직접 본다. 헤더가 없으면 https로
      본다 — uds는 앞단이 TLS를 맡는 구성이라는 것이 이 서버의 전제이고,
      평문 프록시라면 `X-Forwarded-Proto: http`를 붙여 뒤집을 수 있다.
      틀렸을 때 조용히 새는 쪽(Secure 누락)보다 바로 드러나는 쪽(브라우저가
      쿠키를 버려 로그인이 안 됨)으로 기울인다.
    - 그 밖에는 uvicorn이 보정해 준 scheme을 그대로 믿는다. TCP로 리슨하는
      경우 프록시는 같은 호스트에 있고 forwarded_allow_ips 기본값(127.0.0.1)에
      걸리므로 보정이 정상 동작한다.
    """
    if config.uds:
        return conn.headers.get("x-forwarded-proto", "https").lower() in ("https", "wss")
    if config.tls_enabled:
        return True
    return conn.url.scheme in ("https", "wss")


def origin_allowed(conn) -> bool:
    origin = conn.headers.get("origin")
    if not origin:
        return False
    origin = origin.rstrip("/").lower()
    if config.allowed_origins:
        return origin in config.allowed_origins
    host = conn.headers.get("host")
    if not host:
        return False
    parts = urlsplit(origin)
    if parts.netloc != host.lower():
        return False
    # 호스트만 비교하면 표준 포트로 서비스할 때 http://<호스트>와
    # https://<호스트>의 netloc이 같아 **평문 오리진이 통과한다.** 테일넷 안에
    # 들어온 공격자가 그 호스트명의 80포트를 잡으면 만들 수 있는 페이지이고,
    # 거기서 연 wss:// 요청에는 Secure 쿠키가 그대로 실린다 — 쿠키는 페이지의
    # 스킴이 아니라 요청의 스킴을 보기 때문이다.
    # 반대로 평문으로 들어온 요청에는 스킴을 강제하지 않는다. 앞단 프록시가
    # TLS를 끝내고 평문으로 넘겨주는 구성이 정확히 그렇게 보이기 때문이다.
    return parts.scheme == "https" if _is_secure(conn) else True


@app.post("/api/login")
async def login(request: Request):
    return await auth.login(
        request,
        origin_allowed=origin_allowed,
        request_is_secure=_is_secure(request),
    )


@app.post("/api/logout")
async def logout(request: Request):
    return await auth.logout(
        request,
        origin_allowed=origin_allowed,
        request_is_secure=_is_secure(request),
    )


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/projects")
async def list_projects(request: Request):
    """상태 채널과 같은 현재 스냅샷을 반환하는 단발성 API."""
    if not auth.is_authed(request.cookies):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await project_status.snapshot()


@app.websocket("/api/projects/ws")
async def project_status_ws(ws: WebSocket):
    """인증된 프로젝트 상태 푸시 채널. 클라이언트 입력은 모두 무시한다."""
    client = _peer(ws)
    if not origin_allowed(ws):
        _audit_throttled(
            "project-status-ws-reject", client, "origin",
            origin=ws.headers.get("origin"), host=ws.headers.get("host"),
        )
        await ws.close(code=4403, reason="허용되지 않은 오리진")
        return

    await ws.accept()
    if not auth.is_authed(ws.cookies):
        _audit_throttled("project-status-ws-reject", client, "unauthorized")
        await ws.close(code=4401, reason="인증 필요")
        return

    watchdog = auth.register_socket(ws)

    project_status.add(ws)
    _audit("project-status-ws-open", client=client)
    try:
        while True:
            try:
                message = await ws.receive()
            except RuntimeError:
                # 로그아웃/만료 watchdog이 이미 닫은 경우.
                break
            if message["type"] == "websocket.disconnect":
                break
            # 이 채널은 서버→클라이언트 전용이다. 그 밖의 프레임은 조용히 무시한다.
    except WebSocketDisconnect:
        pass
    finally:
        project_status.discard(ws)
        auth.unregister_socket(ws, watchdog)
        _audit("project-status-ws-close", client=client)


WS_MODES = ("attach", "new", "resume", "continue")
AGENTS = ("claude", "codex", "shell")


@app.post("/api/session/end")
async def end_session(request: Request, project: str, agent: str = "claude"):
    """라이브 세션을 지금 종료한다 (사이드바의 "종료" 버튼).

    이전에는 세션을 끝낼 방법이 "새 세션"으로 덮어쓰거나 유예가 만료되기를
    기다리는 것뿐이었다. 탭을 닫는 것은 종료가 아니다 — 그건 소켓만 떼는
    것이고, 그래야 다시 열었을 때 화면이 복원된다.

    POST이고 Origin 검사를 탄다(/api/logout과 같은 형태). GET이면 다른 사이트가
    <img>만으로 남의 세션을 끊을 수 있고, Origin을 보지 않으면 그 페이지의
    fetch 한 줄이면 된다.

    검사 순서는 WS 라우트와 같다: 인증이 화이트리스트보다 먼저다. 반대로 두면
    인증 없는 상대가 404/400 여부로 어떤 프로젝트가 있는지 떠볼 수 있다.
    """
    client = _peer(request)
    # 인증 전 두 거절은 기록을 접는다 — _audit_throttled 주석 참고.
    if not origin_allowed(request):
        _audit_throttled(
            "session-end-reject", client, "origin",
            origin=request.headers.get("origin"), host=request.headers.get("host"),
        )
        return JSONResponse({"ok": False}, status_code=403)
    if not auth.is_authed(request.cookies):
        _audit_throttled("session-end-reject", client, "unauthorized", project=project)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # 여기부터는 인증된 호출자만 도달한다 — 접지 않고 그대로 남긴다. 세션을 끊는
    # 경로라 "무엇을 끊으려 했는지"까지 조사에 필요하고, WS 라우트도 같은 거절을
    # 전부 기록한다.
    if config.find_project(project) is None:
        _audit("session-end-reject", reason="unknown-project", client=client, project=project)
        return JSONResponse({"ok": False}, status_code=404)
    if agent not in AGENTS:
        _audit("session-end-reject", reason="bad-agent", client=client, agent=agent)
        return JSONResponse({"ok": False}, status_code=400)
    # 이미 없는 세션이어도 200이다. 종료 버튼을 누른 사람이 원한 상태가 곧
    # 그것이고, 라이브 여부는 클라이언트가 받은 직전 스냅샷과 어긋날 수 있다.
    ended = await manager.end(f"{project}#{agent}")
    _audit("session-end", client=client, project=project, agent=agent, ended=ended)
    return JSONResponse({"ok": True, "ended": ended})


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
    # 쿠키 검사보다 먼저. 다른 사이트가 연 소켓은 쿠키가 유효해도 붙여선 안 된다.
    # accept 전에 닫으므로 핸드셰이크 자체가 HTTP 403으로 끝난다 — 공격자 페이지는
    # 열린 소켓을 단 한 순간도 쥐지 못한다. 대신 close code가 전달되지 않아
    # 브라우저에는 평범한 연결 실패로만 보이므로, 설정 실수를 가려낼 단서는
    # 로그로 남긴다(정상 사용 중에는 찍힐 일이 없는 줄이다).
    client = _peer(ws)
    if not origin_allowed(ws):
        # 인증 전이라 기록을 접는다 — _audit_throttled 주석 참고.
        _audit_throttled(
            "ws-reject", client, "origin",
            origin=ws.headers.get("origin"), host=ws.headers.get("host"),
        )
        await ws.close(code=4403, reason="허용되지 않은 오리진")
        return

    # 나머지 거절은 accept 뒤에 닫는다 — 그래야 close code가 클라이언트까지 전달돼
    # 앱이 원인을 알고 재연결을 멈춘다(4403만 위에서 예외적으로 먼저 닫는다).
    await ws.accept()

    # 인증을 프로젝트/에이전트 검사보다 먼저 본다. 순서가 반대면 인증 없는 상대가
    # 4404/4400 여부로 화이트리스트에 무엇이 있는지 떠볼 수 있다.
    if not auth.is_authed(ws.cookies):
        _audit_throttled("ws-reject", client, "unauthorized", project=project_name)
        await ws.close(code=4401, reason="인증 필요")
        return

    project = config.find_project(project_name)
    if project is None:
        _audit("ws-reject", reason="unknown-project", client=client, project=project_name)
        await ws.close(code=4404, reason="화이트리스트에 없는 프로젝트")
        return

    if shell:
        agent = "shell"
    if agent not in AGENTS:
        _audit("ws-reject", reason="bad-agent", client=client, agent=agent)
        await ws.close(code=4400, reason="지원하지 않는 에이전트")
        return

    # mode도 값을 확인한다. 아래에서 알 수 없는 값은 attach로 떨어지므로 동작에는
    # 문제가 없지만, 검증 없이 감사 기록에 실리는 필드를 남겨두지 않는다.
    if mode not in WS_MODES:
        _audit("ws-reject", reason="bad-mode", client=client, mode=mode)
        await ws.close(code=4400, reason="지원하지 않는 모드")
        return

    session_key = f"{project_name}#{agent}"
    async with manager.transition(session_key):
        session = manager.get_live(session_key)
        if session is not None and mode != "new":
            action = "reattach"
            await session.attach(ws)
            session.send_status("실행 중인 세션에 재접속했습니다.")
        elif agent == "shell":
            action = "start"
            session = await manager.start_locked(
                session_key, project.path, ssh=project.ssh, agent="shell",
                project_env=project.env,
            )
            await session.attach(ws)
            session.send_status("셸 세션을 시작합니다.")
        else:
            if mode not in ("resume", "continue"):
                has_history = False
            elif project.ssh is not None:
                history = await remote_has_history(project.ssh, project.path, agent)
                if history is HistoryState.ERROR:
                    message = (
                        "원격 세션 기록을 확인하지 못했습니다. 연결·인증·호스트 키를 "
                        "확인한 뒤 다시 시도하세요."
                    )
                    await ws.send_text(json.dumps({"type": "status", "message": message}))
                    await ws.close(
                        code=HISTORY_CHECK_CLOSE_CODE,
                        reason="원격 기록 확인 실패",
                    )
                    _audit(
                        "ws-open-failed", client=client, project=project_name,
                        agent=agent, mode=mode, reason="history-check", remote=project.ssh,
                    )
                    return
                has_history = history is HistoryState.PRESENT
            elif agent == "codex":
                has_history = await has_codex_history(project.path)
            else:
                has_history = latest_session_id(project.path) is not None
            if mode == "resume" and has_history:
                action = "resume"
                extra_args = ["resume"] if agent == "codex" else ["--resume"]
                msg = "이어할 세션을 목록에서 선택하세요."
            elif mode == "continue" and has_history:
                action = "continue"
                extra_args = ["resume", "--last"] if agent == "codex" else ["--continue"]
                msg = "가장 최근 세션을 이어합니다."
            elif mode in ("resume", "continue"):
                action = "start"
                extra_args, msg = None, "이어할 세션 기록이 없어 새 세션을 시작합니다."
            else:
                action = "start"
                extra_args, msg = None, "새 세션을 시작합니다."
            session = await manager.start_locked(
                session_key, project.path, extra_args, ssh=project.ssh, agent=agent,
                command_args=project.args_for(agent), project_env=project.env,
            )
            await session.attach(ws)
            session.send_status(msg)

    # 사후 조사에서 가장 중요한 줄. "어느 시각에 어느 프로젝트에서 무엇이 떴나"가
    # 여기 남는다. 실행된 명령이나 터미널 내용은 남기지 않는다.
    _audit(
        "ws-open", client=client, project=project_name, agent=agent,
        mode=mode, action=action, remote=project.ssh,
    )
    opened_at = time.monotonic()

    # 이 소켓을 연 토큰을 기록해 두면 로그아웃이 여기까지 닿는다. 인증이 꺼진
    # 구성(password_hash 없음)에는 폐기할 토큰 자체가 없으므로 건너뛴다.
    watchdog = auth.register_socket(
        ws, lambda code, reason: session.close_attached(ws, code, reason)
    )

    # 프로토콜 경계다. 여기서 걸러지지 않은 형식 오류는 전부 루프 밖으로 나가
    # 세션을 끊는다 — 자기 세션만 끊는 자해라 보안 문제는 아니지만, 버그난
    # 클라이언트가 원인 모를 단절을 겪고 서버 로그에는 트레이스백만 쌓인다.
    # 잘못된 JSON을 이미 조용히 무시하고 있으므로 나머지도 같은 규칙으로 맞춘다.
    try:
        while True:
            try:
                raw = await ws.receive_text()
            except RuntimeError:
                # 폐기(로그아웃/만료)로 다른 태스크가 이 소켓을 이미 닫아둔 경우.
                # Starlette은 닫힌 소켓에서의 receive를 RuntimeError로 알린다.
                # 정상 종료 경로이므로 트레이스백을 남기지 않는다.
                break
            except KeyError:
                # 바이너리 프레임. receive_text는 message["text"]를 그냥 꺼내므로
                # KeyError가 된다. 서버→클라이언트만 바이너리를 쓴다.
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue  # "5"나 "[]"도 유효한 JSON이다
            kind = msg.get("type")
            if kind == "input":
                data = msg.get("data")
                if isinstance(data, str) and session.write_input(data):
                    # 자식이 stdin을 읽지 않아 입력이 버려졌다. 조용히 버리면
                    # 타이핑이 사라진 것처럼 보인다.
                    session.send_status(
                        "입력이 밀려 일부를 버렸습니다 (세션이 입력을 읽지 않는 중)."
                    )
            elif kind == "resize":
                cols, rows = msg.get("cols"), msg.get("rows")
                # bool도 int이지만 resize가 값을 범위로 자르므로 해가 없다.
                if isinstance(cols, int) and isinstance(rows, int):
                    session.resize(cols, rows)
    except WebSocketDisconnect:
        pass
    finally:
        auth.unregister_socket(ws, watchdog)
        await session.detach(ws)
        _audit(
            "ws-close", client=client, project=project_name, agent=agent,
            seconds=int(time.monotonic() - opened_at),
        )


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
    except Exception:
        # 새 파일이 깨져 있어도 기존 컨텍스트는 온전하므로 서비스는 계속된다.
        # 개인키 경로가 예외 문자열에 섞여 로그로 나가지 않게 상세값은 남기지 않는다.
        _tls_log.error("인증서 리로드 실패, 기존 인증서를 유지합니다")


def _setup_tls():
    """SSLContext를 만들어 보관하고 SIGHUP 훅을 건 뒤, uvicorn에 넘길 팩토리를 반환."""
    global _ssl_ctx
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        _load_cert(ctx)
    except Exception:
        # 기동은 실패시키되 ssl 예외가 개인키 경로를 출력하지 않게 경계를 정리한다.
        raise RuntimeError("TLS 인증서/개인키를 읽지 못했습니다") from None
    _ssl_ctx = ctx
    # uvicorn이 가로채는 시그널은 SIGINT/SIGTERM뿐이라 SIGHUP 핸들러는 살아남는다.
    signal.signal(signal.SIGHUP, _on_sighup)
    return lambda cfg, default_factory: ctx


# ── PID 파일 ────────────────────────────────────────────────────────
#
# 서버가 직접 쓴다. start.sh(백그라운드 기동)와 launchd(포그라운드 기동) 어느
# 쪽으로 띄워도 같은 파일이 나와야 stop.sh와 인증서 --reloadcmd가 동작한다.
#
# 동시에 이 파일이 "서버는 하나"라는 잠금이기도 하다. 예전에는 pid를 무조건
# 덮어썼는데, 그러면 다른 포트로 두 번째 서버가 뜨는 순간 첫 번째의 pid가 파일에서
# 사라져 stop.sh도 인증서 리로드도 그 프로세스에 영원히 닿지 못했다. 같은 포트면
# 바인딩 실패로 두 번째가 죽지만 포트가 다르면 둘 다 정상 기동하므로, 포트 충돌에
# 기대지 않고 여기서 직접 막는다.
#
# 방법은 `fcntl.lockf`(POSIX 권고 잠금)다. 파일에 적힌 pid를 보고 `kill -0`으로
# 살아있는지 확인하는 방식과 달리, 프로세스가 어떻게 죽든 커널이 잠금을 놓아주므로
# stale pid 판정 자체가 필요 없다. 잠금은 프로세스 단위라 fork한 PTY 자식은
# 물려받지 않고, `os.open`은 기본이 close-on-exec이라 exec된 claude/셸에도 남지 않는다.

_pid_fd: int | None = None

EXIT_ALREADY_RUNNING = 3

_pid_log = logging.getLogger("wterm.pid")
_pid_log.setLevel(logging.INFO)  # 루트가 ERROR로 잠겨 있다 — _tls_log 쪽 주석 참조


def _claim_pid_file() -> None:
    """pid 파일을 배타 잠금으로 점유한다. 이미 서버가 떠 있으면 기동을 거부한다."""
    global _pid_fd
    PID_FILE.parent.mkdir(exist_ok=True)
    # 잠금은 경로가 아니라 inode에 걸린다. 우리가 여는 것과 잠근 뒤 경로가 가리키는
    # 것이 같은 파일인지 확인해야, 그 사이에 누가 pid 파일을 지우고 새로 만든 경우
    # (start.sh/stop.sh의 rm) 서로 다른 inode를 잠근 두 서버가 동시에 살아남지 않는다.
    for _ in range(5):
        fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                raise
            other = os.pread(fd, 32, 0).decode("utf-8", "replace").strip() or "?"
            os.close(fd)
            _pid_log.error(
                "이미 서버가 실행 중입니다 (pid %s) — 기동을 중단합니다. "
                "먼저 ./stop.sh 로 내리세요: %s",
                other,
                PID_FILE,
            )
            raise SystemExit(EXIT_ALREADY_RUNNING)
        try:
            if os.fstat(fd).st_ino == os.stat(PID_FILE).st_ino:
                break
        except FileNotFoundError:
            pass
        os.close(fd)  # 우리가 잠근 파일은 이미 경로에서 떨어져 나갔다. 다시 연다.
    else:
        _pid_log.error("pid 파일이 계속 교체되어 점유하지 못했습니다: %s", PID_FILE)
        raise SystemExit(EXIT_ALREADY_RUNNING)

    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    os.fsync(fd)
    _pid_fd = fd
    atexit.register(_release_pid_file)


def _release_pid_file() -> None:
    # 내가 쓴 pid일 때만 지운다. 늦게 죽는 이전 프로세스가 새 서버의 pid 파일을
    # 지워버리는 상황을 막기 위한 것. 확인은 반드시 잠금을 쥔 fd로 한다 —
    # 같은 파일을 새로 open했다가 close하면 그 순간 이 프로세스의 잠금이 풀린다
    # (POSIX 권고 잠금의 특성). fd를 닫는 것은 종료 직전이므로 그냥 열어둔다.
    global _pid_fd
    if _pid_fd is None:
        return
    fd, _pid_fd = _pid_fd, None
    try:
        if os.pread(fd, 32, 0).decode("utf-8", "replace").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


# ── 종료 상태 ───────────────────────────────────────────────────────
#
# uvicorn(0.34+)은 graceful shutdown을 마친 뒤 잡아뒀던 시그널을 원래 핸들러에게
# 다시 던진다(Server._reraise_signals). 원래 핸들러가 기본값이면 프로세스는 결국
# "SIGTERM에 맞아 죽은" 상태로 끝나고, 이건 감시자 입장에서 비정상 종료다:
# launchd의 KeepAlive(SuccessfulExit=false)가 그대로 걸려 stop.sh로 내린 서버를
# 곧바로 되살린다 — stop.sh가 아무 효과도 없어 보이는 원인이었다.
# 미리 우리 핸들러를 걸어두면 uvicorn이 복원한 뒤 되던진 SIGTERM을 여기서 받아
# 종료 코드 0으로 끝낼 수 있다. 이 시점에는 lifespan shutdown이 이미 끝나
# 세션 정리(manager.shutdown)도 완료된 상태다.


def _exit_success(signum, frame) -> None:
    _audit("server-stop", pid=os.getpid())
    _release_pid_file()  # os._exit는 atexit를 타지 않으므로 직접 정리한다
    os._exit(0)


def setup_logging() -> None:
    """ERROR 이상만 기록. 자정마다 로테이션하며 하루 치(직전 파일 1개)만 보관.

    감사 로그(wterm.audit)만 별도 파일에 장기 보관한다 — 위 보관 주기로는
    사후 조사가 불가능하기 때문이다.
    """
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    # 감사 로그에는 접속 주소와 프로젝트 이름이 남고, wterm.out에는 기동 시
    # 설정 경고가 남는다. 로테이션으로 새로 생기는 파일까지 한 번에 덮으려면
    # 파일마다 chmod하는 것보다 디렉터리를 닫는 쪽이 확실하다.
    try:
        log_dir.chmod(0o700)
    except OSError:
        pass  # 권한이 없으면(다른 사용자 소유 등) 로깅 자체를 막지는 않는다
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

    setup_audit_logging(log_dir, fmt)


# ── 비밀 파일 권한 ──────────────────────────────────────────────────
#
# projects.json에는 argon2 해시가 들어 있고, 그건 오프라인 크래킹 대상이다.
# logs/는 기동할 때마다 700으로 조이면서(setup_logging) 정작 설정 파일은 보지
# 않았다 — 점검이 scripts/cert-status.sh에만 있는데 그건 TLS를 쓰지 않으면
# 실행할 이유가 없는 스크립트다.
#
# 경고만 하고 chmod는 하지 않는다. 배포 설정의 권한을 서버가 말없이 바꾸는 것은
# 다른 종류의 사고를 만든다 (앞단 프록시나 배포 도구가 그 파일을 읽고 있을 수 있다).
_config_log = logging.getLogger("wterm.config")
_config_log.setLevel(logging.INFO)  # 루트가 ERROR로 잠겨 있다 — _tls_log 주석 참조


def _warn_on_config_permissions() -> None:
    try:
        mode = CONFIG_PATH.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        _config_log.warning(
            "%s 권한이 %04o입니다 — 같은 머신의 다른 사용자가 읽을 수 있습니다. "
            "password_hash가 들어 있으면 오프라인 크래킹 대상입니다: chmod 600 %s",
            CONFIG_PATH, mode, CONFIG_PATH,
        )


def main() -> None:
    import uvicorn

    setup_logging()
    _warn_on_config_permissions()
    _claim_pid_file()
    # 재시작은 모든 토큰과 PTY 세션이 끊기는 경계다. 감사 기록을 읽을 때
    # 이 줄이 없으면 "그 시각 이후 기록이 왜 비었는지"를 알 수 없다.
    _audit(
        "server-start", pid=os.getpid(),
        listen=config.uds or f"{config.host}:{config.port}",
        tls=config.tls_enabled and not config.uds,
        auth=config.password_hash is not None,
    )
    # uvicorn이 serve() 진입 시 이 핸들러를 저장했다가 종료 직전에 복원하고
    # SIGTERM을 되던진다. 반드시 uvicorn.run() 전에 걸어야 한다.
    signal.signal(signal.SIGTERM, _exit_success)
    # log_config=None: uvicorn 자체 로깅 설정을 끄고 위 root 로거로 전파시킴.
    # 구현 선택을 auto에 맡기면 설치된 extra와 uvicorn 릴리즈에 따라 이벤트 루프나
    # HTTP/WS 프로토콜이 조용히 바뀐다. macOS에서 그 조합의 TLS WSS 업그레이드가
    # 간헐적으로 멎은 적이 있으므로 CI와 운영이 검증한 조합을 명시한다.
    kwargs = {
        "log_config": None,
        "access_log": False,
        "loop": "asyncio",
        "http": "httptools",
        "ws": "websockets-sansio",
    }
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
