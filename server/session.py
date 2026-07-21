"""PTY 기반 Claude Code 세션 관리.

- 프로젝트(cwd)당 최대 1개의 라이브 세션을 유지한다 (온디맨드).
- WebSocket이 끊겨도 유예 시간(grace_seconds) 동안 프로세스를 유지하고,
  재연결되면 그대로 이어붙인다. 유예 시간이 지나면 SIGTERM → SIGKILL 순으로
  프로세스 그룹 전체를 종료한다.
- 재연결 시 화면 복원을 위해 최근 출력(최대 BUFFER_LIMIT 바이트)을 버퍼링한다.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import shlex
import signal
import struct
import termios
from pathlib import Path

from fastapi import WebSocket

BUFFER_LIMIT = 256 * 1024  # 재연결 replay 버퍼 상한
READ_CHUNK = 65536
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
SIGTERM_WAIT = 10  # SIGTERM 후 SIGKILL까지 대기 초

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def munge_cwd(cwd: str) -> str:
    """Claude Code가 ~/.claude/projects/ 하위 디렉터리명으로 쓰는 경로 변환."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def latest_session_id(cwd: str) -> str | None:
    """해당 cwd의 가장 최근 Claude Code 세션 ID를 조회한다."""
    project_dir = CLAUDE_PROJECTS_DIR / munge_cwd(cwd)
    if not project_dir.is_dir():
        return None
    jsonls = sorted(
        project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return jsonls[0].stem if jsonls else None


def has_codex_history(cwd: str) -> bool:
    """Codex 세션 메타데이터에서 해당 cwd의 대화 기록 존재 여부를 확인한다."""
    if not CODEX_SESSIONS_DIR.is_dir():
        return False
    for path in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as f:
                meta = json.loads(f.readline())
            if (
                meta.get("type") == "session_meta"
                and meta.get("payload", {}).get("cwd") == cwd
            ):
                return True
        except (OSError, json.JSONDecodeError):
            continue
    return False


async def remote_has_history(ssh: str, cwd: str, agent: str = "claude") -> bool:
    """원격 호스트에 해당 cwd의 Claude/Codex 세션 기록이 있는지 확인한다.

    비대화식 확인이므로 BatchMode를 쓴다 — 키 인증이 안 돼 있으면 False가 되어
    새 세션으로 폴백한다 (스폰 자체는 대화식이라 터미널에서 패스워드 입력 가능).
    """
    if agent == "codex":
        needle = json.dumps({"cwd": cwd}, ensure_ascii=False, separators=(",", ":"))[1:-1]
        check_cmd = (
            f"grep -rlF -m1 -- {shlex.quote(needle)} ~/.codex/sessions "
            ">/dev/null 2>&1"
        )
    else:
        check_cmd = f"ls ~/.claude/projects/{munge_cwd(cwd)}/*.jsonl"
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ssh,
        check_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


class Session:
    """하나의 claude PTY 프로세스와 그에 붙은 WebSocket을 관리한다."""

    def __init__(
        self,
        project_name: str,
        cwd: str,
        grace_seconds: int,
        ssh: str | None = None,
        agent: str = "claude",
    ):
        self.project_name = project_name
        self.cwd = cwd
        self.grace_seconds = grace_seconds
        self.ssh = ssh
        self.agent = agent  # "claude" | "codex" | "shell"
        self.pid: int = -1
        self.master_fd: int = -1
        self.alive = False
        self.buffer = bytearray()
        self.websocket: WebSocket | None = None
        self._grace_task: asyncio.Task | None = None
        self._loop = asyncio.get_running_loop()
        self._write_buffer = bytearray()
        self._writer_registered = False

    # ── 프로세스 수명 주기 ────────────────────────────────────────────

    def spawn(self, extra_args: list[str] | None = None) -> None:
        base_cmd = (
            ["bash", "-l"]
            if self.agent == "shell"
            else [self.agent, *(extra_args or [])]
        )
        if self.ssh is not None:
            # -t: 원격에 TTY 강제 할당. resize(SIGWINCH)·시그널은 ssh가 중계하고,
            # ssh가 끊기면(grace 만료 포함) 원격 프로세스는 SIGHUP으로 정리된다.
            # BatchMode는 쓰지 않는다 — 패스워드/호스트키 프롬프트를 터미널에서 처리 가능.
            remote_cmd = f"cd {shlex.quote(self.cwd)} && exec {shlex.join(base_cmd)}"
            if self.agent != "shell":
                # bash -lc: 비대화식 ssh는 ~/.profile을 안 읽어 ~/.local/bin 등이
                # PATH에 없으므로 로그인 셸로 감싸 claude를 찾게 한다.
                # (셸 모드는 bash -l 자체가 로그인 셸이라 불필요)
                remote_cmd = f"exec bash -lc {shlex.quote(remote_cmd)}"
            cmd = ["ssh", "-t", self.ssh, remote_cmd]
        else:
            cmd = base_cmd

        pid, master_fd = pty.fork()  # 자식은 setsid + PTY를 제어 터미널로 가짐
        if pid == 0:  # 자식 프로세스
            try:
                if self.ssh is None:
                    os.chdir(self.cwd)  # 원격 경로는 로컬에 없음 — ssh 명령이 cd 수행
                env = dict(os.environ, TERM="xterm-256color")
                os.execvpe(cmd[0], cmd, env)
            except Exception:
                os._exit(127)

        self.pid = pid
        self.master_fd = master_fd
        self.alive = True
        os.set_blocking(master_fd, False)
        self._loop.add_reader(master_fd, self._on_pty_readable)

    def _on_pty_readable(self) -> None:
        try:
            data = os.read(self.master_fd, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:  # EIO: 자식 종료로 slave가 닫힘
            data = b""
        if not data:
            asyncio.ensure_future(self._handle_exit())
            return
        self.buffer.extend(data)
        if len(self.buffer) > BUFFER_LIMIT:
            del self.buffer[: len(self.buffer) - BUFFER_LIMIT]
        if self.websocket is not None:
            asyncio.ensure_future(self._send_bytes(data))

    async def _send_bytes(self, data: bytes) -> None:
        ws = self.websocket
        if ws is None:
            return
        try:
            await ws.send_bytes(data)
        except Exception:
            pass  # 전송 실패는 disconnect 핸들러가 처리

    async def _handle_exit(self) -> None:
        if not self.alive:
            return
        self.alive = False
        self._cleanup_fd()
        exit_code = self._reap()
        ws = self.websocket
        self.websocket = None
        if ws is not None:
            try:
                await ws.send_text(json.dumps({"type": "exit", "code": exit_code}))
                await ws.close()
            except Exception:
                pass

    def _cleanup_fd(self) -> None:
        if self.master_fd >= 0:
            try:
                self._loop.remove_reader(self.master_fd)
            except Exception:
                pass
            if self._writer_registered:
                try:
                    self._loop.remove_writer(self.master_fd)
                except Exception:
                    pass
                self._writer_registered = False
            self._write_buffer.clear()
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1

    def _reap(self) -> int | None:
        try:
            _, status = os.waitpid(self.pid, 0)
            return os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            return None

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass

    async def terminate(self) -> None:
        """SIGTERM → 대기 → SIGKILL 순으로 프로세스 그룹을 종료한다."""
        if not self.alive:
            return
        self._signal_group(signal.SIGTERM)
        for _ in range(SIGTERM_WAIT * 10):
            if not self.alive:
                return
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if pid != 0:
                break
            await asyncio.sleep(0.1)
        else:
            self._signal_group(signal.SIGKILL)
        self.alive = False
        self._cleanup_fd()

    # ── WebSocket 연결/해제 ──────────────────────────────────────────

    async def attach(self, ws: WebSocket) -> None:
        """WebSocket을 세션에 연결한다. 기존 연결이 있으면 끊고 교체한다."""
        self.cancel_grace()
        old = self.websocket
        self.websocket = ws
        if old is not None:
            try:
                await old.close(code=4000, reason="다른 클라이언트가 연결됨")
            except Exception:
                pass
        if self.buffer:
            await self._send_bytes(bytes(self.buffer))

    def detach(self, ws: WebSocket) -> None:
        """연결 해제. 유예 타이머를 시작한다."""
        if self.websocket is not ws:
            return  # 이미 다른 연결로 교체됨
        self.websocket = None
        if self.alive:
            self._grace_task = asyncio.ensure_future(self._grace_countdown())

    def cancel_grace(self) -> None:
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None

    async def _grace_countdown(self) -> None:
        try:
            await asyncio.sleep(self.grace_seconds)
        except asyncio.CancelledError:
            return
        await self.terminate()

    # ── 입력/리사이즈 ────────────────────────────────────────────────

    def write_input(self, data: str) -> None:
        """PTY master fd에 입력을 쓴다.

        master_fd는 non-blocking이라 os.write()가 부분 쓰기(반환값 < len)로
        끝나거나 EAGAIN(BlockingIOError)을 던질 수 있다 (claude가 stdin을
        읽지 못할 만큼 출력 처리에 바쁠 때 등). 반환값을 무시하고 예외를
        삼키기만 하면 한글처럼 문자당 여러 바이트(UTF-8 3바이트)인 입력이
        중간에 잘려 깨진다 — 못 쓴 나머지는 버퍼에 남겨두고 add_writer로
        fd가 다시 쓰기 가능해질 때 이어서 쓴다.
        """
        if self.alive and self.master_fd >= 0:
            self._write_buffer.extend(data.encode())
            self._flush_write()

    def _flush_write(self) -> None:
        if self.master_fd < 0:
            return
        while self._write_buffer:
            try:
                n = os.write(self.master_fd, self._write_buffer)
            except BlockingIOError:
                break
            except OSError:
                self._write_buffer.clear()
                break
            del self._write_buffer[:n]
        if self._write_buffer:
            if not self._writer_registered:
                self._loop.add_writer(self.master_fd, self._flush_write)
                self._writer_registered = True
        elif self._writer_registered:
            self._loop.remove_writer(self.master_fd)
            self._writer_registered = False

    def resize(self, cols: int, rows: int) -> None:
        if self.alive and self.master_fd >= 0:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            try:
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass


class SessionManager:
    """프로젝트 이름 → 라이브 세션 매핑."""

    def __init__(self, grace_seconds: int):
        self.grace_seconds = grace_seconds
        self.sessions: dict[str, Session] = {}

    def get_live(self, project_name: str) -> Session | None:
        s = self.sessions.get(project_name)
        if s is not None and not s.alive:
            del self.sessions[project_name]
            return None
        return s

    async def start(
        self,
        project_name: str,
        cwd: str,
        extra_args: list[str] | None = None,
        ssh: str | None = None,
        agent: str = "claude",
    ) -> Session:
        """새 claude/셸 프로세스를 기동한다. 같은 키의 라이브 세션이 있으면 먼저 종료한다."""
        existing = self.get_live(project_name)
        if existing is not None:
            await existing.terminate()
            del self.sessions[project_name]

        session = Session(project_name, cwd, self.grace_seconds, ssh=ssh, agent=agent)
        session.spawn(extra_args)
        self.sessions[project_name] = session
        return session

    async def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            s.cancel_grace()
            await s.terminate()
        self.sessions.clear()
