"""PTY 기반 Claude Code 세션 관리.

- 프로젝트(cwd)당 최대 1개의 라이브 세션을 유지한다 (온디맨드).
- WebSocket이 끊겨도 유예 시간(grace_seconds) 동안 프로세스를 유지하고,
  재연결되면 그대로 이어붙인다. 유예 시간이 지나면 SIGTERM → SIGKILL 순으로
  프로세스 그룹 전체를 종료한다.
- 재연결 시 화면 복원을 위해 최근 출력(최대 BUFFER_LIMIT 바이트)을 버퍼링한다.
- idle_seconds가 켜져 있으면 양방향 트래픽이 그 시간 동안 없는 세션을 종료한다
  (기본값 0 = 끔). grace는 **연결이 끊겨야** 도는 타이머라 탭을 열어둔 채 잊은
  세션은 잡지 못한다.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import pwd
import re
import shlex
import signal
import struct
import termios
import time
from pathlib import Path

from anyio import to_thread
from fastapi import WebSocket

BUFFER_LIMIT = 256 * 1024  # 재연결 replay 버퍼 상한
# PTY 쓰기 대기 버퍼 상한. 자식이 stdin을 읽지 않는 동안(출력 처리에 바쁘거나
# 페이저처럼 입력을 기다리지 않는 상태) 들어오는 입력이 여기 쌓인다. 출력 쪽은
# BUFFER_LIMIT으로 이미 잘리는데 입력 쪽만 열려 있으면 세션 하나가 서버 프로세스
# 전체를 OOM으로 끌고 갈 수 있다.
WRITE_BUFFER_LIMIT = 1024 * 1024
READ_CHUNK = 65536
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
SIGTERM_WAIT = 10  # SIGTERM 후 SIGKILL까지 대기 초

# 유휴 종료 close code. 4404/4400과 같은 취급을 받아야 한다 — 클라이언트가 사유를
# 보여주고 재연결을 멈춰야지, 자동 재연결이 돌면 방금 정리한 세션이 곧바로 다시 뜬다.
IDLE_CLOSE_CODE = 4408
IDLE_MIN_SLEEP = 0.5  # 남은 시간이 0에 가까울 때 바쁜 루프가 되지 않도록

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def login_shell() -> str:
    """이 사용자의 로그인 셸 경로.

    $SHELL을 먼저 보되 없으면 passwd 엔트리로 떨어진다. 데몬(launchd/systemd)은
    로그인 셸을 거치지 않아 $SHELL을 넣어주지 않으므로, 이게 없으면 zsh 사용자가
    부팅 자동 기동 환경에서만 bash 셸을 받게 된다 — 재현하기 까다로운 종류의 차이라
    환경변수에 의존하지 않고 passwd에서 직접 읽는다.
    """
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
    except KeyError:
        return "/bin/sh"


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


# Codex 세션 파일은 ~/.codex/sessions 아래에 쌓이기만 하는 구조다. 프로젝트마다
# 전체를 rglob으로 훑으면 /api/projects 한 번이 O(파일 수 × 프로젝트 수)가 되고,
# 프론트는 그 목록을 10초마다 폴링한다 — 시간이 지날수록 느려지는 형태다. 게다가
# 동기 파일 I/O라 그동안 살아있는 모든 PTY 세션의 입출력이 멎는다(argon2 검증을
# 스레드로 뺀 것과 정확히 같은 이유의 문제다).
#
# 그래서 세 가지를 함께 한다: 한 번의 스캔으로 cwd 집합을 통째로 만들고(프로젝트
# 수와 무관해진다), 결과를 폴링 주기보다 넉넉히 길게 캐시하고, 스캔 자체는
# 스레드에서 돌린다. 대가는 방금 생긴 기록이 최대 CODEX_CACHE_TTL초 늦게 보이는
# 것인데, 그 사이 그 세션은 라이브라 재접속 경로를 타므로 기록 조회에 닿지 않는다.
CODEX_CACHE_TTL = 30.0

_codex_cwds: set[str] = set()
_codex_scanned_at = float("-inf")
_codex_scan_lock = asyncio.Lock()  # 폴링이 겹칠 때 같은 스캔을 두 번 돌리지 않는다


def _scan_codex_cwds() -> set[str]:
    """세션 메타데이터 첫 줄에서 cwd를 모은다. 동기 함수 — 스레드에서 부를 것."""
    cwds: set[str] = set()
    if not CODEX_SESSIONS_DIR.is_dir():
        return cwds
    for path in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as f:
                meta = json.loads(f.readline())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        # 첫 줄이 유효한 JSON이라고 해서 객체라는 보장은 없다 ("5"도 JSON이다).
        if not isinstance(meta, dict) or meta.get("type") != "session_meta":
            continue
        payload = meta.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
            cwds.add(payload["cwd"])
    return cwds


async def has_codex_history(cwd: str) -> bool:
    """해당 cwd에 Codex 대화 기록이 있는지. 결과는 CODEX_CACHE_TTL초 캐시된다."""
    global _codex_cwds, _codex_scanned_at
    async with _codex_scan_lock:
        if time.monotonic() - _codex_scanned_at >= CODEX_CACHE_TTL:
            _codex_cwds = await to_thread.run_sync(_scan_codex_cwds)
            _codex_scanned_at = time.monotonic()
    return cwd in _codex_cwds


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
        idle_seconds: int = 0,
    ):
        self.project_name = project_name
        self.cwd = cwd
        self.grace_seconds = grace_seconds
        self.idle_seconds = idle_seconds
        self.ssh = ssh
        self.agent = agent  # "claude" | "codex" | "shell"
        self.pid: int = -1
        self.master_fd: int = -1
        self.alive = False
        self.buffer = bytearray()
        self.websocket: WebSocket | None = None
        self._grace_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._loop = asyncio.get_running_loop()
        self.last_activity = time.monotonic()
        self._write_buffer = bytearray()
        self._writer_registered = False
        self._input_dropped = False

    # ── 프로세스 수명 주기 ────────────────────────────────────────────

    def spawn(self, extra_args: list[str] | None = None) -> None:
        if self.ssh is not None:
            # 원격 셸은 로컬 $SHELL 경로가 원격 머신에 존재한다는 보장이 없으므로
            # bash로 고정한다.
            base_cmd = (
                ["bash", "-l"]
                if self.agent == "shell"
                else [self.agent, *(extra_args or [])]
            )
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
            # 로컬 셸은 사용자의 로그인 셸(macOS 기본값 zsh, 리눅스는 배포판마다
            # 다름)을 존중한다.
            cmd = (
                [login_shell(), "-l"]
                if self.agent == "shell"
                else [self.agent, *(extra_args or [])]
            )

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
        self.last_activity = time.monotonic()
        os.set_blocking(master_fd, False)
        self._loop.add_reader(master_fd, self._on_pty_readable)
        if self.idle_seconds > 0:
            self._idle_task = asyncio.ensure_future(self._idle_countdown())

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
        self.last_activity = time.monotonic()
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
        self._cancel_idle()
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
            self._input_dropped = False
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
        self._cancel_idle()
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
            # SIGKILL 뒤에도 거둬가지 않으면 좀비가 남는다. 서버는 오래 사는
            # 프로세스라 유예 만료로 강제 종료된 세션마다 하나씩 쌓인다.
            # SIGKILL은 즉시 반영되므로 짧게만 기다린다.
            for _ in range(10):
                try:
                    if os.waitpid(self.pid, os.WNOHANG)[0] != 0:
                        break
                except ChildProcessError:
                    break
                await asyncio.sleep(0.1)
        self.alive = False
        self._cleanup_fd()

    # ── WebSocket 연결/해제 ──────────────────────────────────────────

    async def attach(self, ws: WebSocket) -> None:
        """WebSocket을 세션에 연결한다. 기존 연결이 있으면 끊고 교체한다."""
        self.cancel_grace()
        self.last_activity = time.monotonic()
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

    # ── 유휴 종료 ────────────────────────────────────────────────────
    #
    # grace는 연결이 끊겨야 도는 타이머라, 탭을 열어둔 채 잊은 세션은 잡지 못한다.
    # 그 세션은 사람이 다시 볼 때까지 무기한 살면서 자격증명이 붙은 claude 프로세스를
    # 계속 물고 있다.
    #
    # 활동은 **양방향 트래픽**으로 센다: 클라이언트 입력, PTY 출력, 그리고 재접속.
    # 입력만 세면 사람 없이 몇 시간을 도는 정상적인 자동 실행이 유휴로 잡혀 죽는다 —
    # 그쪽이 훨씬 비싼 오답이라, 출력이 계속 나오는 세션(`tail -f` 같은)은 영원히
    # 살아남는 쪽을 택했다. 잊힌 세션은 양쪽 다 조용하다는 것이 이 판정의 근거다.

    def _cancel_idle(self) -> None:
        task, self._idle_task = self._idle_task, None
        # 유휴 만료 자신이 terminate를 부르는 경로가 있다. 자기 자신을 취소하면
        # 그 자리에서 CancelledError가 나 뒷정리가 끊긴다.
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _idle_countdown(self) -> None:
        try:
            while True:
                remaining = self.idle_seconds - (time.monotonic() - self.last_activity)
                if remaining <= 0:
                    break
                await asyncio.sleep(max(remaining, IDLE_MIN_SLEEP))
        except asyncio.CancelledError:
            return
        self._idle_task = None  # 아래 terminate가 자신을 취소하지 않도록
        await self._expire_idle()

    async def _expire_idle(self) -> None:
        # 알림이 먼저다. terminate는 대화형 셸처럼 SIGTERM을 무시하는 자식에게
        # SIGTERM_WAIT초를 꽉 쓰는데, 그 뒤에 알리면 화면이 그동안 아무 이유 없이
        # 멎어 있다. 소켓을 먼저 떼어 두면 라우트의 detach는 그대로 빠져나가고
        # (유예 타이머도 걸리지 않는다) 종료는 이 태스크가 마저 끝낸다.
        ws = self.websocket
        self.websocket = None
        if ws is not None:
            span = (
                f"{self.idle_seconds // 60}분"
                if self.idle_seconds >= 60
                else f"{self.idle_seconds}초"
            )
            try:
                await ws.send_text(json.dumps({
                    "type": "status",
                    "message": f"{span} 동안 아무 움직임이 없어 세션을 종료했습니다.",
                }))
                await ws.close(code=IDLE_CLOSE_CODE, reason="유휴 상태로 종료됨")
            except Exception:
                pass  # 이미 끊긴 소켓. 라우트 쪽 finally가 정리한다
        await self.terminate()

    # ── 입력/리사이즈 ────────────────────────────────────────────────

    def write_input(self, data: str) -> bool:
        """PTY master fd에 입력을 쓴다. 넘쳐서 버렸으면 True를 반환한다.

        master_fd는 non-blocking이라 os.write()가 부분 쓰기(반환값 < len)로
        끝나거나 EAGAIN(BlockingIOError)을 던질 수 있다 (claude가 stdin을
        읽지 못할 만큼 출력 처리에 바쁠 때 등). 반환값을 무시하고 예외를
        삼키기만 하면 한글처럼 문자당 여러 바이트(UTF-8 3바이트)인 입력이
        중간에 잘려 깨진다 — 못 쓴 나머지는 버퍼에 남겨두고 add_writer로
        fd가 다시 쓰기 가능해질 때 이어서 쓴다.

        그 버퍼가 WRITE_BUFFER_LIMIT을 넘으면 **새로 들어온 입력을** 통째로
        버린다. 앞을 버리면 이미 받아둔 UTF-8 멀티바이트가 잘려 한글 입력이
        깨지고, 그건 이 버퍼가 존재하는 이유 자체를 무너뜨린다. 한 메시지는
        통째로 받거나 통째로 버려서 경계에서도 문자가 갈라지지 않게 한다.

        반환값은 "넘침이 시작된 순간"에만 True다 — 호출자가 상태 메시지를
        한 번만 보내도록. 버퍼가 다 빠지면 다시 알릴 수 있는 상태로 돌아간다.
        """
        if not (self.alive and self.master_fd >= 0):
            return False
        self.last_activity = time.monotonic()
        if len(self._write_buffer) >= WRITE_BUFFER_LIMIT:
            first = not self._input_dropped
            self._input_dropped = True
            return first
        self._write_buffer.extend(data.encode())
        self._flush_write()
        return False

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
        else:
            self._input_dropped = False  # 다 빠졌으니 다음 넘침은 다시 알린다
            if self._writer_registered:
                self._loop.remove_writer(self.master_fd)
                self._writer_registered = False

    def resize(self, cols: int, rows: int) -> None:
        """터미널 크기를 PTY에 반영한다.

        값은 winsize에 담을 수 있는 범위로 자른다. struct.pack("HHHH", ...)은
        0..65535를 벗어나면 struct.error를 던지는데, 이 호출은 클라이언트가 보낸
        숫자를 그대로 받는 자리라 그러면 메시지 하나로 세션이 끊긴다.
        """
        if not (self.alive and self.master_fd >= 0):
            return
        winsize = struct.pack(
            "HHHH", max(0, min(rows, 0xFFFF)), max(0, min(cols, 0xFFFF)), 0, 0
        )
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass


class SessionManager:
    """프로젝트 이름 → 라이브 세션 매핑."""

    def __init__(self, grace_seconds: int, idle_seconds: int = 0):
        self.grace_seconds = grace_seconds
        self.idle_seconds = idle_seconds
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

        session = Session(
            project_name, cwd, self.grace_seconds,
            ssh=ssh, agent=agent, idle_seconds=self.idle_seconds,
        )
        session.spawn(extra_args)
        self.sessions[project_name] = session
        return session

    async def shutdown(self) -> None:
        # 세션마다 SIGTERM 후 최대 SIGTERM_WAIT초를 기다리므로 순차로 돌리면
        # 종료 시간이 세션 수에 비례해 늘어난다. 감시자(launchd/systemd)와
        # stop.sh 모두 종료 대기에 상한이 있고, 그 상한을 넘겨 SIGKILL을 맞으면
        # 비정상 종료로 보여 곧바로 되살아난다 — 총 대기를 SIGTERM_WAIT로 묶는다.
        sessions = list(self.sessions.values())
        for s in sessions:
            s.cancel_grace()
        if sessions:
            await asyncio.gather(*(s.terminate() for s in sessions))
        self.sessions.clear()
