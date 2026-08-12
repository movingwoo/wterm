"""프로젝트 목록 스냅샷과 상태 WebSocket broadcast.

라우트의 Origin/인증/감사 경계는 main.py가 소유하고, 이 모듈은 인증을 통과한
구독자 집합과 상태 계산만 맡는다. 브라우저 수와 무관하게 파일 이력을 한 번 계산해
같은 직렬화 결과를 fan-out하는 것이 이 경계의 핵심이다.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

from .session import CODEX_CACHE_TTL, has_codex_history, latest_session_id

PROJECT_STATUS_REFRESH_INTERVAL = CODEX_CACHE_TTL


class ProjectStatusHub:
    """세션 변화는 즉시, 외부 기록 변화는 저빈도로 모든 구독자에게 알린다."""

    def __init__(self, config, manager):
        self.config = config
        self.manager = manager
        self._dirty = asyncio.Event()
        self._sockets: set[WebSocket] = set()
        self._log = logging.getLogger("wterm.projects")

    def changed(self) -> None:
        """세션의 라이브 판정이 바뀌었을 때 broadcaster를 깨운다."""
        self._dirty.set()

    def add(self, ws: WebSocket) -> None:
        self._sockets.add(ws)
        self.changed()  # 첫 스냅샷도 단일 계산 경로를 탄다

    def discard(self, ws: WebSocket) -> None:
        self._sockets.discard(ws)

    async def snapshot(self) -> list[dict[str, object]]:
        """화이트리스트 프로젝트와 라이브/기록 상태의 한 시점 스냅샷."""
        result: list[dict[str, object]] = []
        for project in self.config.projects:
            result.append(
                {
                    "name": project.name,
                    "path": project.path,
                    "ssh": project.ssh,
                    "live": self.manager.get_live(
                        f"{project.name}#claude"
                    ) is not None,
                    "codex_live": self.manager.get_live(
                        f"{project.name}#codex"
                    ) is not None,
                    "shell_live": self.manager.get_live(
                        f"{project.name}#shell"
                    ) is not None,
                    # 원격 기록 확인은 ssh를 동반하므로 터미널 접속 시 수행한다.
                    "has_history": (
                        True
                        if project.ssh
                        else latest_session_id(project.path) is not None
                    ),
                    "codex_has_history": (
                        True
                        if project.ssh
                        else await has_codex_history(project.path)
                    ),
                }
            )
        return result

    async def _broadcast(self, payload: str) -> None:
        sockets = list(self._sockets)
        if not sockets:
            return
        results = await asyncio.gather(
            *(ws.send_text(payload) for ws in sockets), return_exceptions=True
        )
        for ws, result in zip(sockets, results):
            if isinstance(result, BaseException):
                self._sockets.discard(ws)

    async def run(self) -> None:
        """구독자가 있을 때만 계산하고, 같은 주기 결과는 다시 보내지 않는다."""
        last_payload: str | None = None
        while True:
            triggered = False
            if not self._sockets:
                await self._dirty.wait()
                self._dirty.clear()
                if not self._sockets:
                    continue
                triggered = True
            else:
                try:
                    await asyncio.wait_for(
                        self._dirty.wait(),
                        timeout=PROJECT_STATUS_REFRESH_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    pass
                else:
                    self._dirty.clear()
                    triggered = True

            if not self._sockets:
                continue
            try:
                payload = json.dumps(
                    {"type": "projects", "projects": await self.snapshot()},
                    ensure_ascii=False,
                )
            except Exception:
                # 조회 실패 하나로 이 태스크가 죽으면 이후 세션 변화도 사라진다.
                self._log.exception("프로젝트 상태 스냅샷 계산 실패")
                continue
            if triggered or payload != last_payload:
                await self._broadcast(payload)
            last_payload = payload
