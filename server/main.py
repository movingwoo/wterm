"""W-Term: Claude Code 웹 원격 제어 서버.

실행: .venv/bin/uvicorn server.main:app --host <host> --port <port>
또는: .venv/bin/python -m server  (projects.json의 host/port 사용)
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import secrets
import signal
import ssl
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from anyio import to_thread
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
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


def origin_allowed(headers) -> bool:
    origin = headers.get("origin")
    if not origin:
        return False
    origin = origin.rstrip("/").lower()
    if config.allowed_origins:
        return origin in config.allowed_origins
    host = headers.get("host")
    return bool(host) and urlsplit(origin).netloc == host.lower()


# ── 패스워드 인증 (projects.json에 password_hash가 있을 때만) ──────────
#
# 로그인 성공 시 발급한 토큰을 서버 메모리에만 보관한다 (무상태 철학 유지,
# 서버 재시작 시 전부 무효화되어 재로그인 필요).
#
# 만료는 쿠키 max-age가 아니라 서버가 판정한다. max-age는 브라우저에게 하는 부탁일
# 뿐이라, 탈취된 토큰은 그것만으로는 영원히 유효하다. 발급 시각을 함께 들고 있으면서
# 검사할 때마다 확인하고, 폐기 수단으로 /api/logout을 둔다 (재시작은 폐기 수단이 될
# 수 없다 — PTY 세션이 전부 죽는다).
AUTH_COOKIE = "wterm_token"
AUTH_TOKEN_TTL = 30 * 24 * 3600  # 쿠키 max-age와 서버측 만료를 같은 값으로 묶는다
MAX_TOKENS = 512  # 발급 토큰 상한. 넘으면 오래된 것부터 버린다

# 시계 변경에 영향받지 않도록 monotonic을 쓴다. 토큰은 어차피 프로세스와 수명을
# 같이 하므로 벽시계 기준으로 보관할 이유가 없다.
_valid_tokens: OrderedDict[str, float] = OrderedDict()  # token -> 발급 시각
_password_hasher = PasswordHasher()

_auth_log = logging.getLogger("wterm.auth")
# 루트가 ERROR로 잠겨 있어(setup_logging 참조) 그대로 두면 차단 기록이 남지 않는다.
# 차단은 장애가 아니지만 "왜 로그인이 안 되지"의 답이 여기밖에 없다. wterm.tls와
# 같은 방식으로 자식 로거에 자체 레벨을 준다.
_auth_log.setLevel(logging.INFO)


def _issue_token() -> str:
    now = time.monotonic()
    for token in [t for t, at in _valid_tokens.items() if now - at >= AUTH_TOKEN_TTL]:
        del _valid_tokens[token]
    token = secrets.token_urlsafe(32)
    _valid_tokens[token] = now
    while len(_valid_tokens) > MAX_TOKENS:
        _valid_tokens.popitem(last=False)  # 가장 먼저 발급된 것부터
    return token


def is_authed(cookies: dict[str, str]) -> bool:
    if config.password_hash is None:
        return True
    token = cookies.get(AUTH_COOKIE)
    if token is None:
        return False
    issued_at = _valid_tokens.get(token)
    if issued_at is None:
        return False
    if time.monotonic() - issued_at >= AUTH_TOKEN_TTL:
        del _valid_tokens[token]
        return False
    return True


def _is_secure(request: Request) -> bool:
    """https로 들어온 요청인지. 앞단 프록시가 TLS를 끝내는 구성에서는 uvicorn이
    X-Forwarded-Proto로 scheme을 바로잡아 준다. 서버가 직접 TLS를 종단하는 경우
    평문으로 들어올 길 자체가 없으므로 tls_enabled면 무조건 True로 봐도 된다."""
    return request.url.scheme == "https" or config.tls_enabled


def _set_auth_cookie(resp: Response, request: Request, token: str) -> None:
    # samesite=strict: 인증 확인은 같은 오리진에서 뜬 페이지의 fetch/WS만 하므로
    # 교차 사이트 진입 흐름이 없다. 첫 문서 요청(/)은 쿠키를 보지 않는 덕에
    # 외부 링크로 들어와도 화면이 깨지지 않는다.
    resp.set_cookie(
        AUTH_COOKIE,
        token,
        max_age=AUTH_TOKEN_TTL,
        httponly=True,
        samesite="strict",
        secure=_is_secure(request),
    )


# ── 로그인 시도 제한 ────────────────────────────────────────────────
#
# argon2 검증 1회가 64 MiB / 35ms를 쓴다. 제한이 없으면 인증 없이 누구나 그 비용을
# 태울 수 있고, 검증을 스레드로 뺀 뒤에도 비용 자체는 그대로다. 그래서 두 겹으로 막는다:
#   (1) 버킷별 실패 횟수에 지수 백오프 — 반복 시도를 검증 전에 잘라낸다
#   (2) 전역 동시 검증 수 제한 — IP를 흩뿌리는 시도에도 총비용의 상한을 준다
# (1)만으로는 분산 시도를 막지 못하고 (2)만으로는 무차별 대입을 막지 못한다.

LOGIN_FREE_ATTEMPTS = 5  # 이 횟수까지는 지연 없음 (오타 여유)
LOGIN_BACKOFF_MAX = 900.0  # 차단 상한 15분
LOGIN_FAIL_DECAY = 900.0  # 마지막 차단이 풀린 뒤 이만큼 조용하면 실패 횟수 리셋
LOGIN_BUCKET_LIMIT = 1024  # 추적할 버킷 상한 (IP를 바꿔가며 채우는 것 방지)
LOGIN_MAX_CONCURRENT = 2  # 동시 argon2 검증 수

_login_fails: OrderedDict[str, tuple[int, float]] = OrderedDict()  # 키 -> (실패수, 해제시각)
_login_slots = asyncio.Semaphore(LOGIN_MAX_CONCURRENT)


def _client_key(request: Request) -> str:
    """제한 버킷 키. uds나 프록시 뒤에서는 실제 클라이언트 IP가 없거나 프록시
    IP뿐이라 전부 한 버킷으로 모인다 — 그 구성에서는 앞단이 IP별 제한을 맡고,
    여기서는 전역 제한으로만 동작한다."""
    return request.client.host if request.client else "-"


def _login_block_remaining(key: str, now: float) -> float:
    """남은 차단 시간(초). 0이면 통과."""
    entry = _login_fails.get(key)
    return max(0.0, entry[1] - now) if entry else 0.0


def _record_login_failure(key: str, now: float) -> None:
    fails, unblock_at = _login_fails.pop(key, (0, 0.0))
    if now - unblock_at > LOGIN_FAIL_DECAY:
        fails = 0  # 한참 조용했으면 새로 센다 (오타가 영구 누적되지 않도록)
    fails += 1
    delay = (
        min(2.0 ** (fails - LOGIN_FREE_ATTEMPTS), LOGIN_BACKOFF_MAX)
        if fails > LOGIN_FREE_ATTEMPTS
        else 0.0
    )
    if delay:
        _auth_log.info("로그인 %d회 실패로 %.0f초 차단: %s", fails, delay, key)
    _login_fails[key] = (fails, now + delay)  # pop 후 재삽입이라 이미 맨 뒤(=최신)
    while len(_login_fails) > LOGIN_BUCKET_LIMIT:
        _login_fails.popitem(last=False)


@app.post("/api/login")
async def login(request: Request):
    """패스워드 검증 후 HttpOnly 쿠키로 세션 토큰을 발급한다."""
    if not origin_allowed(request.headers):
        return JSONResponse({"ok": False}, status_code=403)
    if config.password_hash is None:
        return JSONResponse({"ok": True})
    key = _client_key(request)
    wait = _login_block_remaining(key, time.monotonic())
    if wait > 0:
        # 차단 중에는 argon2를 아예 돌리지 않는다. 비용을 안 태우는 것이 요점이다.
        retry_after = int(wait) + 1
        return JSONResponse(
            {"ok": False, "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    try:
        body = await request.json()
        password = str(body["password"])
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        return JSONResponse({"ok": False}, status_code=400)
    try:
        async with _login_slots:
            # argon2 검증은 64 MiB짜리 동기 CPU 작업이다. 이벤트 루프에서 그대로
            # 돌리면 검증 한 번마다 살아있는 모든 PTY 세션의 입출력이 멎는다.
            await to_thread.run_sync(
                _password_hasher.verify, config.password_hash, password
            )
    except (VerifyMismatchError, InvalidHash):
        _record_login_failure(key, time.monotonic())
        return JSONResponse({"ok": False}, status_code=401)
    _login_fails.pop(key, None)
    resp = JSONResponse({"ok": True})
    _set_auth_cookie(resp, request, _issue_token())
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    """토큰을 서버에서 지우고 쿠키를 만료시킨다."""
    if not origin_allowed(request.headers):
        return JSONResponse({"ok": False}, status_code=403)
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        _valid_tokens.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(
        AUTH_COOKIE, httponly=True, samesite="strict", secure=_is_secure(request)
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
    # 쿠키 검사보다 먼저. 다른 사이트가 연 소켓은 쿠키가 유효해도 붙여선 안 된다.
    # accept 전에 닫으므로 핸드셰이크 자체가 HTTP 403으로 끝난다 — 공격자 페이지는
    # 열린 소켓을 단 한 순간도 쥐지 못한다. 대신 close code가 전달되지 않아
    # 브라우저에는 평범한 연결 실패로만 보이므로, 설정 실수를 가려낼 단서는
    # 로그로 남긴다(정상 사용 중에는 찍힐 일이 없는 줄이다).
    if not origin_allowed(ws.headers):
        _auth_log.info(
            "오리진 불일치로 WS 거절: origin=%r host=%r",
            ws.headers.get("origin"), ws.headers.get("host"),
        )
        await ws.close(code=4403, reason="허용되지 않은 오리진")
        return

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
    _release_pid_file()  # os._exit는 atexit를 타지 않으므로 직접 정리한다
    os._exit(0)


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
    # uvicorn이 serve() 진입 시 이 핸들러를 저장했다가 종료 직전에 복원하고
    # SIGTERM을 되던진다. 반드시 uvicorn.run() 전에 걸어야 한다.
    signal.signal(signal.SIGTERM, _exit_success)
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
