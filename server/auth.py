"""메모리 토큰 인증, 열린 WebSocket 폐기, 로그인 비용 제한."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from anyio import to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse, Response

from .audit import audit, peer

AUTH_COOKIE = "wterm_token"
AUTH_TOKEN_TTL = 30 * 24 * 3600
MAX_TOKENS = 512
AUTH_RECHECK_INTERVAL = 30.0

LOGIN_FREE_ATTEMPTS = 5
LOGIN_BACKOFF_MAX = 900.0
LOGIN_FAIL_DECAY = 900.0
LOGIN_BUCKET_LIMIT = 1024
LOGIN_MAX_CONCURRENT = 2
LOGIN_BODY_LIMIT = 4096


class AuthManager:
    """프로세스와 수명을 같이 하는 인증 상태를 한 곳에서 관리한다."""

    def __init__(self, password_hash: str | None):
        self.password_hash = password_hash
        self._valid_tokens: OrderedDict[bytes, float] = OrderedDict()
        self._password_hasher = PasswordHasher()
        self._tokens_changed = asyncio.Event()
        self._ws_tokens: dict[WebSocket, bytes] = {}
        self._ws_closers: dict[
            WebSocket, Callable[[int, str], Awaitable[None]]
        ] = {}
        self._login_fails: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._login_slots = asyncio.Semaphore(LOGIN_MAX_CONCURRENT)

    @staticmethod
    def _token_key(token: str) -> bytes:
        """원문 토큰을 저장·조회 키로 쓰지 않는다."""
        return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()

    def _issue_token(self) -> str:
        now = time.monotonic()
        removed = False
        for key in [
            key
            for key, issued_at in self._valid_tokens.items()
            if now - issued_at >= AUTH_TOKEN_TTL
        ]:
            del self._valid_tokens[key]
            removed = True
        token = secrets.token_urlsafe(32)
        self._valid_tokens[self._token_key(token)] = now
        while len(self._valid_tokens) > MAX_TOKENS:
            self._valid_tokens.popitem(last=False)
            removed = True
        if removed:
            self._tokens_changed.set()
        return token

    def _key_valid(self, key: bytes) -> bool:
        issued_at = self._valid_tokens.get(key)
        if issued_at is None:
            return False
        if time.monotonic() - issued_at >= AUTH_TOKEN_TTL:
            del self._valid_tokens[key]
            self._tokens_changed.set()
            return False
        return True

    def is_authed(self, cookies: dict[str, str]) -> bool:
        if self.password_hash is None:
            return True
        token = cookies.get(AUTH_COOKIE)
        return token is not None and self._key_valid(self._token_key(token))

    async def _close_unauthed(self, ws: WebSocket, reason: str) -> None:
        closer = self._ws_closers.get(ws)
        try:
            if closer is not None:
                await closer(4401, reason)
            else:
                await ws.close(code=4401, reason=reason)
        except Exception:
            pass

    async def _revoke_token_sockets(self, key: bytes) -> int:
        victims = [ws for ws, socket_key in self._ws_tokens.items() if socket_key == key]
        for ws in victims:
            await self._close_unauthed(ws, "세션이 폐기되었습니다")
        return len(victims)

    async def _watchdog(self, ws: WebSocket, key: bytes) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._tokens_changed.wait(), timeout=AUTH_RECHECK_INTERVAL
                )
            except asyncio.TimeoutError:
                pass
            else:
                self._tokens_changed.clear()
            if not self._key_valid(key):
                await self._close_unauthed(ws, "인증이 만료되었습니다")
                return

    def register_socket(
        self,
        ws: WebSocket,
        close: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> asyncio.Task | None:
        """인증된 소켓을 로그아웃/만료 폐기 대상에 넣는다."""
        token = ws.cookies.get(AUTH_COOKIE) if self.password_hash is not None else None
        if not token:
            return None
        key = self._token_key(token)
        self._ws_tokens[ws] = key
        if close is not None:
            self._ws_closers[ws] = close
        return asyncio.ensure_future(self._watchdog(ws, key))

    def unregister_socket(self, ws: WebSocket, watchdog: asyncio.Task | None) -> None:
        if watchdog is not None:
            watchdog.cancel()
        self._ws_tokens.pop(ws, None)
        self._ws_closers.pop(ws, None)

    def _login_block_remaining(self, key: str, now: float) -> float:
        entry = self._login_fails.get(key)
        return max(0.0, entry[1] - now) if entry else 0.0

    def _record_login_failure(self, key: str, now: float) -> None:
        fails, unblock_at = self._login_fails.pop(key, (0, 0.0))
        if now - unblock_at > LOGIN_FAIL_DECAY:
            fails = 0
        fails += 1
        delay = (
            min(2.0 ** (fails - LOGIN_FREE_ATTEMPTS), LOGIN_BACKOFF_MAX)
            if fails > LOGIN_FREE_ATTEMPTS
            else 0.0
        )
        if delay:
            audit("login-blocked", client=key, fails=fails, seconds=int(delay))
        self._login_fails[key] = (fails, now + delay)
        while len(self._login_fails) > LOGIN_BUCKET_LIMIT:
            self._login_fails.popitem(last=False)

    @staticmethod
    def _set_auth_cookie(
        response: Response, request_is_secure: bool, token: str
    ) -> None:
        response.set_cookie(
            AUTH_COOKIE,
            token,
            max_age=AUTH_TOKEN_TTL,
            httponly=True,
            samesite="strict",
            secure=request_is_secure,
        )

    async def login(
        self,
        request: Request,
        *,
        origin_allowed: Callable[[Request], bool],
        request_is_secure: bool,
    ) -> Response:
        """Origin 확인부터 argon2 검증과 쿠키 발급까지의 로그인 경계."""
        key = peer(request)
        if not origin_allowed(request):
            audit(
                "login-reject",
                reason="origin",
                client=key,
                origin=request.headers.get("origin"),
                host=request.headers.get("host"),
            )
            return JSONResponse({"ok": False}, status_code=403)
        if self.password_hash is None:
            return JSONResponse({"ok": True})

        declared = request.headers.get("content-length")
        if declared is None or not declared.isdigit():
            return JSONResponse({"ok": False}, status_code=411)
        if int(declared) > LOGIN_BODY_LIMIT:
            return JSONResponse({"ok": False}, status_code=413)
        wait = self._login_block_remaining(key, time.monotonic())
        if wait > 0:
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
            async with self._login_slots:
                await to_thread.run_sync(
                    self._password_hasher.verify, self.password_hash, password
                )
        except (VerifyMismatchError, InvalidHash):
            audit("login-fail", client=key)
            self._record_login_failure(key, time.monotonic())
            return JSONResponse({"ok": False}, status_code=401)

        self._login_fails.pop(key, None)
        audit("login-ok", client=key)
        response = JSONResponse({"ok": True})
        self._set_auth_cookie(response, request_is_secure, self._issue_token())
        return response

    async def logout(
        self,
        request: Request,
        *,
        origin_allowed: Callable[[Request], bool],
        request_is_secure: bool,
    ) -> Response:
        """현재 토큰을 폐기하고 그 토큰으로 열린 소켓을 즉시 닫는다."""
        if not origin_allowed(request):
            return JSONResponse({"ok": False}, status_code=403)
        token = request.cookies.get(AUTH_COOKIE)
        closed = 0
        if token:
            key = self._token_key(token)
            self._valid_tokens.pop(key, None)
            closed = await self._revoke_token_sockets(key)
        audit("logout", client=peer(request), sockets=closed)
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            AUTH_COOKIE,
            httponly=True,
            samesite="strict",
            secure=request_is_secure,
        )
        response.headers["Clear-Site-Data"] = '"cache", "storage"'
        return response
