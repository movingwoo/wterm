"""PTY 기반 Claude Code 세션 관리.

- 프로젝트(cwd)당 최대 1개의 라이브 세션을 유지한다 (온디맨드).
- WebSocket이 끊겨도 유예 시간(grace_seconds) 동안 프로세스를 유지하고,
  재연결되면 그대로 이어붙인다. 유예 시간이 지나면 SIGHUP → SIGTERM → SIGKILL
  순으로 프로세스 그룹 전체를 종료한다.
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
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import Enum
from collections.abc import Callable
from pathlib import Path
from typing import AsyncIterator, Awaitable

from anyio import to_thread
from fastapi import WebSocket

BUFFER_LIMIT = 256 * 1024  # 재연결 replay 버퍼 상한
# PTY 쓰기 대기 버퍼 상한. 자식이 stdin을 읽지 않는 동안(출력 처리에 바쁘거나
# 페이저처럼 입력을 기다리지 않는 상태) 들어오는 입력이 여기 쌓인다. 출력 쪽은
# BUFFER_LIMIT으로 이미 잘리는데 입력 쪽만 열려 있으면 세션 하나가 서버 프로세스
# 전체를 OOM으로 끌고 갈 수 있다.
WRITE_BUFFER_LIMIT = 1024 * 1024
READ_CHUNK = 65536
# 붙은 브라우저가 출력을 받지 못할 때 WebSocket 전송 대기 메모리를 제한한다.
# high에서 PTY reader를 떼면 커널의 PTY 버퍼가 차고, 결국 자식의 write가 막혀
# 스트림을 버리지 않은 채 배압이 전달된다. writer가 low 아래로 비우면 다시 읽는다.
OUTPUT_HIGH_WATERMARK = 1024 * 1024
OUTPUT_LOW_WATERMARK = 512 * 1024
OUTBOUND_CLOSE_TIMEOUT = 3.0
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
SIGTERM_WAIT = 10  # 종료 시그널 에스컬레이션 전체 예산(초). stop.sh의 20초 대기가
                   # 이 값 + SIGKILL 회수를 감안한 것이라 늘리면 그쪽도 같이 봐야 한다.
SIGHUP_WAIT = 2    # 그 예산 중 SIGHUP에 주는 몫. 나머지가 SIGTERM 몫이 된다.

# 유휴 종료 close code. 4404/4400과 같은 취급을 받아야 한다 — 클라이언트가 사유를
# 보여주고 재연결을 멈춰야지, 자동 재연결이 돌면 방금 정리한 세션이 곧바로 다시 뜬다.
IDLE_CLOSE_CODE = 4408
IDLE_MIN_SLEEP = 0.5  # 남은 시간이 0에 가까울 때 바쁜 루프가 되지 않도록

# 사용자가 명시적으로 끝낸 세션. 4408과 같은 이유로 별도 코드다 — 평범한 단절로
# 보이면 app.js의 자동 재연결이 방금 종료한 세션을 곧바로 새로 띄운다.
ENDED_CLOSE_CODE = 4409

# 원격 기록 확인 실패. 새 프로세스로 조용히 폴백하면 사용자가 원래 이어야 할
# 대화를 놓치므로 이 연결은 닫고 명시적인 재시도를 요구한다.
HISTORY_CHECK_CLOSE_CODE = 4410

REMOTE_HISTORY_TIMEOUT = 10.0

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
# 전체를 rglob으로 훑으면 프로젝트 상태 한 번이 O(파일 수 × 프로젝트 수)가 되고,
# 상태 채널은 외부에서 생긴 기록도 잡기 위해 주기적으로 이를 갱신한다 — 시간이
# 지날수록 느려지는 형태다. 게다가
# 동기 파일 I/O라 그동안 살아있는 모든 PTY 세션의 입출력이 멎는다(argon2 검증을
# 스레드로 뺀 것과 정확히 같은 이유의 문제다).
#
# 그래서 세 가지를 함께 한다: 한 번의 스캔으로 cwd 집합을 통째로 만들고(프로젝트
# 수와 무관해진다), 결과를 상태 재검사 주기만큼 캐시하고, 스캔 자체는
# 스레드에서 돌린다. 대가는 방금 생긴 기록이 최대 CODEX_CACHE_TTL초 늦게 보이는
# 것인데, 그 사이 그 세션은 라이브라 재접속 경로를 타므로 기록 조회에 닿지 않는다.
CODEX_CACHE_TTL = 30.0

_codex_cwds: set[str] = set()
_codex_scanned_at = float("-inf")
_codex_scan_lock = asyncio.Lock()  # 상태 조회가 겹칠 때 같은 스캔을 두 번 돌리지 않는다


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


class HistoryState(Enum):
    PRESENT = "present"
    ABSENT = "absent"
    ERROR = "error"


async def _reap_subprocess(proc: asyncio.subprocess.Process) -> None:
    """timeout/cancel된 조회 subprocess를 확실히 거둔다."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 1.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def remote_has_history(
    ssh: str, cwd: str, agent: str = "claude"
) -> HistoryState:
    """원격 호스트에 해당 cwd의 Claude/Codex 세션 기록이 있는지 확인한다.

    비대화식 확인이므로 BatchMode와 StrictHostKeyChecking을 쓴다. 인증/호스트키/
    네트워크/원격 명령 실패는 기록 없음과 구분한다. ConnectTimeout은 연결 단계만
    덮으므로 subprocess 전체에도 별도 timeout을 둔다.
    """
    if agent == "codex":
        needle = json.dumps({"cwd": cwd}, ensure_ascii=False, separators=(",", ":"))[1:-1]
        check_cmd = (
            "[ -d ~/.codex/sessions ] || exit 1; "
            f"grep -rlF -m1 -- {shlex.quote(needle)} ~/.codex/sessions "
            ">/dev/null 2>&1"
        )
    else:
        check_cmd = (
            f"set -- ~/.claude/projects/{munge_cwd(cwd)}/*.jsonl; "
            '[ -e "$1" ]'
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5", ssh,
            check_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return HistoryState.ERROR
    try:
        returncode = await asyncio.wait_for(proc.wait(), REMOTE_HISTORY_TIMEOUT)
    except asyncio.TimeoutError:
        await _reap_subprocess(proc)
        return HistoryState.ERROR
    except asyncio.CancelledError:
        await asyncio.shield(_reap_subprocess(proc))
        raise
    if returncode == 0:
        return HistoryState.PRESENT
    if returncode == 1:
        return HistoryState.ABSENT
    return HistoryState.ERROR


@dataclass
class _Outbound:
    kind: str
    payload: bytes | str | tuple[int, str] | None
    done: asyncio.Future[bool] | None = None


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
        command_args: list[str] | None = None,
        project_env: dict[str, str] | None = None,
        state_changed: Callable[[], None] | None = None,
        grace_expired: Callable[["Session"], Awaitable[None]] | None = None,
        idle_expired: Callable[["Session"], Awaitable[None]] | None = None,
    ):
        self.project_name = project_name
        self.cwd = cwd
        self.grace_seconds = grace_seconds
        self.idle_seconds = idle_seconds
        self.ssh = ssh
        self.agent = agent  # "claude" | "codex" | "shell"
        self.command_args = list(command_args or ())
        self.project_env = dict(project_env or {})
        self._state_changed = state_changed
        self._grace_expired = grace_expired
        self._idle_expired = idle_expired
        self.pid: int = -1
        self.master_fd: int = -1
        self.alive = False
        # terminate()에 들어선 뒤로는 되돌릴 수 없다. alive는 자식이 실제로 거둬질
        # 때까지 True로 남아 있어서(SIGTERM을 무시하는 셸이면 SIGTERM_WAIT초 내내)
        # 그 사이 이 세션은 "살아 있지만 죽는 중"이다 — SessionManager.get_live가
        # 그 상태를 라이브로 내주면 죽어가는 세션에 새 클라이언트가 붙는다.
        self.terminating = False
        self.buffer = bytearray()
        self.websocket: WebSocket | None = None
        self._grace_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._loop = asyncio.get_running_loop()
        self.last_activity = time.monotonic()
        self._write_buffer = bytearray()
        self._writer_registered = False
        self._input_dropped = False
        self._reader_registered = False
        self._pty_reader_paused = False
        self._out_queue: asyncio.Queue[_Outbound] | None = None
        self._out_writer_task: asyncio.Task[None] | None = None
        self._out_writer_ws: WebSocket | None = None
        self._out_pending_bytes = 0
        self._exit_task: asyncio.Task[None] | None = None

    def _notify_state_changed(self) -> None:
        """라이브 여부가 바뀌었음을 알린다. 관찰자 실패가 PTY 수명을 깨면 안 된다."""
        if self._state_changed is None:
            return
        try:
            self._state_changed()
        except Exception:
            pass

    # ── 프로세스 수명 주기 ────────────────────────────────────────────

    def _agent_cmd(self, extra_args: list[str] | None) -> list[str]:
        """에이전트 실행 인자. 알림 채널을 이 세션에 한해 못박는다.

        두 CLI 모두 기본 설정으로는 wterm 안에서 알림을 **아무것도** 내보내지
        않는다. 알림이 없으면 다른 탭을 보는 동안 세션이 권한 프롬프트에서
        멈춘 것을 알 방법이 없고, 그건 이 도구의 용도("덮고 나갔다 돌아온다")가
        걸린 문제다.

        - Claude: 기본값 `auto`는 TERM_PROGRAM으로 채널을 고른다 —
          Apple_Terminal/iTerm/kitty/ghostty가 아니면 "방법 없음"이 되어 조용하다.
          PTY에는 TERM_PROGRAM이 없으니 항상 그쪽이다. xterm.js가 벨로 알아듣는
          것은 terminal_bell(`\\a`) 하나뿐이라 그것으로 고정한다.
        - Codex: `tui.notifications`가 꺼져 있는 것이 기본이다. 켜면 OSC 9로
          내보내는데, 그건 벨이 아니라 OSC 문자열이라 app.js가 따로 받는다.

        사용자의 전역 설정을 건드리지 않고 이 프로세스에만 준다 — 다른 터미널에서
        쓰는 claude/codex의 동작은 그대로여야 한다. 이 플래그가 만들어내는 것은
        wterm의 PTY로 나가는 바이트 하나뿐이고, 그것을 알림으로 띄울지는 이미
        브라우저 쪽 권한과 사이드바의 "알림 켜기"가 정하고 있다.
        """
        notify: list[str] = []
        if self.agent == "claude":
            notify = ["--settings", '{"preferredNotifChannel":"terminal_bell"}']
        elif self.agent == "codex":
            notify = ["-c", "tui.notifications=true"]
        # 프로젝트 옵션은 resume/continue 서브커맨드보다 앞에 와야 하고, 알림 옵션은
        # 그 뒤에 와야 한다. 같은 설정이 중복될 때 대부분의 CLI는 마지막 값을 쓰므로
        # 이 순서가 projects.json의 인자로 알림 불변조건을 끄지 못하게 한다.
        return [self.agent, *self.command_args, *notify, *(extra_args or [])]

    def _command(self, extra_args: list[str] | None = None) -> list[str]:
        """로컬/SSH 실행 argv를 만든다. 원격 셸 문자열의 인코딩은 여기 한 곳뿐이다."""
        if self.ssh is not None:
            # 원격 셸은 로컬 $SHELL 경로가 원격 머신에 존재한다는 보장이 없으므로
            # bash로 고정한다.
            base_cmd = (
                ["bash", "-l"] if self.agent == "shell" else self._agent_cmd(extra_args)
            )
            if self.project_env:
                # ssh의 로컬 프로세스 환경을 바꿔도 서버 설정에 따라 원격으로 전달되지
                # 않는다. env argv를 원격 명령에 명시하고 shlex.join으로 값 전체를 한 번
                # 인용해 공백·따옴표·셸 메타문자가 명령으로 해석되지 않게 한다.
                assignments = [f"{key}={value}" for key, value in self.project_env.items()]
                base_cmd = ["env", *assignments, *base_cmd]
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
                else self._agent_cmd(extra_args)
            )
        return cmd

    def spawn(self, extra_args: list[str] | None = None) -> None:
        cmd = self._command(extra_args)

        pid, master_fd = pty.fork()  # 자식은 setsid + PTY를 제어 터미널로 가짐
        if pid == 0:  # 자식 프로세스
            try:
                if self.ssh is None:
                    os.chdir(self.cwd)  # 원격 경로는 로컬에 없음 — ssh 명령이 cd 수행
                env = dict(os.environ)
                if self.ssh is None:
                    env.update(self.project_env)
                # TERM은 config에서 예약해 두었지만 여기서도 마지막에 못박는다. PTY와
                # xterm.js의 기능 합의라 프로젝트 설정 때문에 달라져서는 안 된다.
                env["TERM"] = "xterm-256color"
                os.execvpe(cmd[0], cmd, env)
            except Exception:
                os._exit(127)

        self.pid = pid
        self.master_fd = master_fd
        self.alive = True
        self.last_activity = time.monotonic()
        os.set_blocking(master_fd, False)
        self._loop.add_reader(master_fd, self._on_pty_readable)
        self._reader_registered = True
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
            if self._reader_registered and self.master_fd >= 0:
                self._loop.remove_reader(self.master_fd)
                self._reader_registered = False
            if self._exit_task is None:
                self._exit_task = asyncio.ensure_future(self._handle_exit())
            return
        self.last_activity = time.monotonic()
        self.buffer.extend(data)
        if len(self.buffer) > BUFFER_LIMIT:
            del self.buffer[: len(self.buffer) - BUFFER_LIMIT]
        if self.websocket is not None:
            self._enqueue_outbound(_Outbound("bytes", data))

    def _pause_pty_reader(self) -> None:
        if self._pty_reader_paused or self.master_fd < 0:
            return
        if self._reader_registered:
            try:
                self._loop.remove_reader(self.master_fd)
            except Exception:
                pass
            self._reader_registered = False
        self._pty_reader_paused = True

    def _resume_pty_reader(self) -> None:
        if not self._pty_reader_paused:
            return
        if self.alive and not self.terminating and self.master_fd >= 0:
            try:
                self._loop.add_reader(self.master_fd, self._on_pty_readable)
            except Exception:
                return
            self._reader_registered = True
        self._pty_reader_paused = False

    def _enqueue_outbound(self, item: _Outbound) -> asyncio.Future[bool] | None:
        """현재 소켓의 단일 writer에 프레임을 넣는다. event loop 안에서만 호출한다."""
        queue = self._out_queue
        if queue is None:
            if item.done is not None and not item.done.done():
                item.done.set_result(False)
            return item.done
        if item.kind == "bytes":
            assert isinstance(item.payload, bytes)
            self._out_pending_bytes += len(item.payload)
            if self._out_pending_bytes >= OUTPUT_HIGH_WATERMARK:
                self._pause_pty_reader()
        queue.put_nowait(item)
        return item.done

    def _queued_control(
        self, kind: str, payload: str | tuple[int, str] | None = None
    ) -> asyncio.Future[bool] | None:
        if self._out_queue is None:
            return None
        done = self._loop.create_future()
        return self._enqueue_outbound(_Outbound(kind, payload, done))

    def send_status(self, message: str) -> None:
        """PTY 출력과 순서가 섞이지 않도록 status도 같은 writer에 넣는다."""
        self._enqueue_outbound(_Outbound("text", json.dumps({
            "type": "status", "message": message,
        })))

    async def _outbound_writer(
        self, ws: WebSocket, queue: asyncio.Queue[_Outbound]
    ) -> None:
        """이 세션의 유일한 WebSocket writer."""
        current: _Outbound | None = None
        try:
            while True:
                current = await queue.get()
                sent = False
                try:
                    if current.kind == "bytes":
                        assert isinstance(current.payload, bytes)
                        await ws.send_bytes(current.payload)
                    elif current.kind == "text":
                        assert isinstance(current.payload, str)
                        await ws.send_text(current.payload)
                    elif current.kind == "close":
                        assert isinstance(current.payload, tuple)
                        await ws.close(code=current.payload[0], reason=current.payload[1])
                    else:
                        raise RuntimeError(f"알 수 없는 outbound 종류: {current.kind}")
                    sent = True
                finally:
                    if current.kind == "bytes":
                        assert isinstance(current.payload, bytes)
                        self._out_pending_bytes -= len(current.payload)
                        if self._out_pending_bytes <= OUTPUT_LOW_WATERMARK:
                            self._resume_pty_reader()
                    if current.done is not None and not current.done.done():
                        current.done.set_result(sent)
                if current.kind == "close":
                    return
                current = None
        except asyncio.CancelledError:
            raise
        except Exception:
            # receive loop가 disconnect를 관찰해 detach한다. 여기서 별도 전송을
            # 시도하면 다시 복수 writer가 된다.
            pass
        finally:
            while True:
                try:
                    pending = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if pending.kind == "bytes":
                    assert isinstance(pending.payload, bytes)
                    self._out_pending_bytes -= len(pending.payload)
                if pending.done is not None and not pending.done.done():
                    pending.done.set_result(False)
            if self._out_pending_bytes <= OUTPUT_LOW_WATERMARK:
                self._resume_pty_reader()
            if self._out_writer_task is asyncio.current_task():
                self._out_queue = None
                self._out_writer_task = None
                self._out_writer_ws = None

    async def _stop_outbound_writer(self, ws: WebSocket) -> None:
        task = self._out_writer_task
        if task is None or self._out_writer_ws is not ws:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _wait_outbound_close(
        self, ws: WebSocket, code: int, reason: str
    ) -> None:
        done = self._queued_control("close", (code, reason))
        if done is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(done), OUTBOUND_CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            # 읽지 않는 브라우저 때문에 자연 종료/명시 종료가 영원히 남아서는 안 된다.
            await self._stop_outbound_writer(ws)
            try:
                await asyncio.wait_for(ws.close(code=code, reason=reason), 1.0)
            except Exception:
                pass

    async def close_attached(self, ws: WebSocket, code: int, reason: str) -> None:
        """인증 watchdog 같은 외부 close도 세션의 writer로 직렬화한다."""
        if self._out_writer_ws is ws:
            await self._wait_outbound_close(ws, code, reason)
            return
        # 이미 교체된 소켓에는 이 세션 writer가 없으므로 동시 send 가능성도 없다.
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass

    async def _handle_exit(self) -> None:
        if not self.alive:
            return
        was_live = not self.terminating
        self.alive = False
        self._cancel_idle()
        self._cleanup_fd()
        exit_code = self._reap()
        ws = self.websocket
        self.websocket = None
        if ws is not None:
            self._enqueue_outbound(_Outbound(
                "text", json.dumps({"type": "exit", "code": exit_code})
            ))
            await self._wait_outbound_close(ws, 1000, "세션이 종료됨")
        # terminate()가 시작된 세션은 그 진입 시점에 이미 라이브 목록에서 빠졌고
        # 그때 알렸다. 자연 종료만 여기서 새 상태 변화를 만든다.
        if was_live:
            self._notify_state_changed()

    def _cleanup_fd(self) -> None:
        if self.master_fd >= 0:
            if self._reader_registered:
                try:
                    self._loop.remove_reader(self.master_fd)
                except Exception:
                    pass
                self._reader_registered = False
            self._pty_reader_paused = False
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

    async def _wait_reaped(self, seconds: float) -> bool:
        """자식이 거둬질 때까지 최대 seconds초 기다린다. 끝났으면 True."""
        for _ in range(int(seconds * 10)):
            # _handle_exit이 먼저 EOF를 보고 거둬갔을 수 있다. 그쪽이 alive와
            # fd 정리를 이미 끝냈으므로 여기서는 끝난 것으로 본다.
            if not self.alive:
                return True
            try:
                pid, _ = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return True  # 겹쳐 들어온 다른 terminate가 거둬갔다
            if pid != 0:
                return True
            await asyncio.sleep(0.1)
        return False

    async def terminate(self) -> None:
        """SIGHUP → SIGTERM → SIGKILL 순으로 프로세스 그룹을 종료한다.

        SIGTERM이 아니라 SIGHUP이 먼저인 이유는 잡 컨트롤이다. 대화형 셸은 자기가
        띄운 잡마다 **별도 프로세스 그룹**을 만드는데, killpg가 때리는 것은 셸의
        그룹뿐이라 잡에는 아무것도 닿지 않는다. 포그라운드 잡은 세션 리더인 셸이
        죽을 때 커널이 보내는 SIGHUP에 걸려 같이 죽지만, 백그라운드 잡은 그것도
        아니어서 고아로 남는다 — SIGKILL을 맞은 셸은 자기 종료 경로(잡 전체에
        HUP 보내기)를 돌 기회가 없기 때문이다. 그래서 사용자가 종료 버튼을 누른
        세션이 `exit`을 친 세션과 달리 프로세스를 남겼다.
        SIGHUP을 받은 셸은 그 종료 경로를 정상적으로 돌므로 잡까지 정리된다.
        덤으로, 대화형 셸은 SIGTERM을 무시해서 종료가 매번 SIGTERM_WAIT초를 꽉
        채웠는데 SIGHUP은 즉시 먹는다.

        세 단계를 합친 대기는 SIGTERM_WAIT을 넘지 않는다. shutdown은 세션을 모두
        동시에 종료하므로 이 상한이 곧 서버 종료 시간의 상한이고, 그 위에 stop.sh와
        감시자의 대기 시간이 잡혀 있다.

        두 경로가 겹쳐 들어와도 안전하다: 시그널이 한 번 더 가고, 자식을 거두는
        것은 둘 중 하나만 성공하며(나머지는 ChildProcessError), _cleanup_fd는
        여러 번 불러도 같다. 그래서 이른 return으로 막지 않는다 — 막으면 먼저
        들어온 쪽이 끝나기 전에 shutdown이 빠져나갈 수 있다.
        """
        if not self.alive:
            return
        first_terminator = not self.terminating
        self.terminating = True
        # 실제 reap까지 SIGTERM_WAIT초가 걸릴 수 있지만, 종료에 들어온 세션은
        # get_live에서 즉시 빠진다. 화면의 라이브 배지도 그 판정과 동시에 내린다.
        if first_terminator:
            self._notify_state_changed()
        self._cancel_idle()
        self._signal_group(signal.SIGHUP)
        if not await self._wait_reaped(SIGHUP_WAIT):
            self._signal_group(signal.SIGTERM)
            if not await self._wait_reaped(SIGTERM_WAIT - SIGHUP_WAIT):
                self._signal_group(signal.SIGKILL)
                # SIGKILL 뒤에도 거둬가지 않으면 좀비가 남는다. 서버는 오래 사는
                # 프로세스라 강제 종료된 세션마다 하나씩 쌓인다. SIGKILL은 즉시
                # 반영되므로 짧게만 기다린다.
                await self._wait_reaped(1)
        self.alive = False
        self._cleanup_fd()

    # ── WebSocket 연결/해제 ──────────────────────────────────────────

    async def attach(self, ws: WebSocket) -> None:
        """WebSocket을 세션에 연결한다. 기존 연결이 있으면 끊고 교체한다."""
        self.cancel_grace()
        self.last_activity = time.monotonic()
        old = self.websocket
        if old is not None:
            self.websocket = None
            await self._stop_outbound_writer(old)
            try:
                await old.close(code=4000, reason="다른 클라이언트가 연결됨")
            except Exception:
                pass
        self.websocket = ws
        self._out_queue = asyncio.Queue()
        self._out_writer_ws = ws
        self._out_writer_task = asyncio.create_task(
            self._outbound_writer(ws, self._out_queue),
            name=f"wterm-output:{self.project_name}",
        )
        if self.buffer:
            replay_done = self._loop.create_future()
            self._enqueue_outbound(_Outbound("bytes", bytes(self.buffer), replay_done))
            # replay 전송 중 들어온 PTY 출력은 같은 큐에서 그 뒤에 붙는다. replay가
            # 끝난 다음 attach가 돌아가야 route의 status가 그 live output 뒤에 놓인다.
            try:
                await replay_done
            except asyncio.CancelledError:
                if self.websocket is ws:
                    self.websocket = None
                await self._stop_outbound_writer(ws)
                raise

    async def detach(self, ws: WebSocket) -> None:
        """연결 해제. 유예 타이머를 시작한다."""
        if self.websocket is not ws:
            return  # 이미 다른 연결로 교체됨
        self.websocket = None
        await self._stop_outbound_writer(ws)
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
        # 여기서부터는 취소받지 않는다. terminate()는 SIGTERM_WAIT초까지 await
        # 하는데, 그 사이 cancel_grace()가 이 태스크를 취소하면 CancelledError가
        # terminate() 한복판에서 튀어나와 alive=False도 _cleanup_fd()도 SIGKILL
        # 후속도 건너뛴다 — SIGTERM만 맞고 살아남은 자식이 마스터 fd를 문 채
        # "라이브"로 남는다. 참조를 먼저 비워 cancel_grace가 손댈 것을 없앤다.
        self._grace_task = None
        if self._grace_expired is not None:
            await self._grace_expired(self)
        else:
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
        if self._idle_expired is not None:
            await self._idle_expired(self)
        else:
            await self._expire_idle()

    async def _expire_idle(self) -> None:
        span = (
            f"{self.idle_seconds // 60}분"
            if self.idle_seconds >= 60
            else f"{self.idle_seconds}초"
        )
        await self._notify_and_terminate(
            f"{span} 동안 아무 움직임이 없어 세션을 종료했습니다.",
            IDLE_CLOSE_CODE,
            "유휴 상태로 종료됨",
        )

    # ── 명시적 종료 ──────────────────────────────────────────────────

    async def end_now(self) -> None:
        """사용자가 요청한 종료. 유예 없이 지금 끝낸다."""
        await self._notify_and_terminate(
            "사용자 요청으로 세션을 종료했습니다.", ENDED_CLOSE_CODE, "사용자가 종료함"
        )

    async def _notify_and_terminate(self, message: str, code: int, reason: str) -> None:
        """붙어 있는 소켓에 사유를 알리고 닫은 뒤 프로세스를 종료한다.

        알림이 먼저다. terminate는 대화형 셸처럼 SIGTERM을 무시하는 자식에게
        SIGTERM_WAIT초를 꽉 쓰는데, 그 뒤에 알리면 화면이 그동안 아무 이유 없이
        멎어 있다. 소켓을 먼저 떼어 두면 라우트의 detach는 그대로 빠져나가고
        (유예 타이머도 걸리지 않는다) 종료는 이 호출이 마저 끝낸다.
        """
        ws = self.websocket
        self.websocket = None
        if ws is not None:
            self._enqueue_outbound(_Outbound("text", json.dumps({
                "type": "status", "message": message,
            })))
            await self._wait_outbound_close(ws, code, reason)
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
        encoded = data.encode()
        if len(self._write_buffer) + len(encoded) > WRITE_BUFFER_LIMIT:
            first = not self._input_dropped
            self._input_dropped = True
            return first
        self._write_buffer.extend(encoded)
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

    def __init__(
        self,
        grace_seconds: int,
        idle_seconds: int = 0,
        state_changed: Callable[[], None] | None = None,
    ):
        self.grace_seconds = grace_seconds
        self.idle_seconds = idle_seconds
        self._state_changed = state_changed
        self.sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        return lock

    @asynccontextmanager
    async def transition(self, session_key: str) -> AsyncIterator[None]:
        """한 세션 키의 판정/history/종료/기동/등록을 직렬화한다."""
        async with self._lock_for(session_key):
            yield

    def get_live(self, project_name: str) -> Session | None:
        s = self.sessions.get(project_name)
        if s is not None and not s.alive:
            del self.sessions[project_name]
            return None
        # 종료 중인 세션은 라이브가 아니다. 유예가 만료된 뒤 SIGTERM_WAIT초 동안은
        # alive가 아직 True인데, 그때 재접속을 붙여주면 곧 죽을 PTY에 화면을
        # 물려주게 된다 (fd가 정리되는 순간 아무 알림 없이 멎는다). 여기서 없는
        # 것으로 답하면 라우트는 새 세션을 띄우고, 이는 "유예가 지났으면 그 세션은
        # 끝난 것"이라는 규칙과도 맞는다. 맵에서 지우지는 않는다 — 종료를 시작한
        # 쪽이 자기가 넣은 항목인지 확인하고 지운다.
        if s is not None and s.terminating:
            return None
        return s

    async def start(
        self,
        project_name: str,
        cwd: str,
        extra_args: list[str] | None = None,
        ssh: str | None = None,
        agent: str = "claude",
        command_args: list[str] | None = None,
        project_env: dict[str, str] | None = None,
    ) -> Session:
        """새 claude/셸 프로세스를 기동한다. 같은 키의 라이브 세션이 있으면 먼저 종료한다."""
        async with self.transition(project_name):
            return await self.start_locked(
                project_name, cwd, extra_args, ssh, agent, command_args, project_env
            )

    async def start_locked(
        self,
        project_name: str,
        cwd: str,
        extra_args: list[str] | None = None,
        ssh: str | None = None,
        agent: str = "claude",
        command_args: list[str] | None = None,
        project_env: dict[str, str] | None = None,
    ) -> Session:
        """transition(project_name)을 이미 잡은 호출자가 쓰는 기동 경로."""
        existing = self.get_live(project_name)
        if existing is not None:
            existing.cancel_grace()
            ws = existing.websocket
            existing.websocket = None
            if ws is not None:
                await existing._stop_outbound_writer(ws)
                try:
                    await ws.close(code=4000, reason="새 세션으로 교체됨")
                except Exception:
                    pass
            await existing.terminate()
            if self.sessions.get(project_name) is existing:
                del self.sessions[project_name]

        session = Session(
            project_name, cwd, self.grace_seconds,
            ssh=ssh, agent=agent, idle_seconds=self.idle_seconds,
            command_args=command_args, project_env=project_env,
            state_changed=self._state_changed,
            grace_expired=lambda expired: self._expire_grace(project_name, expired),
            idle_expired=lambda expired: self._expire_idle(project_name, expired),
        )
        session.spawn(extra_args)
        self.sessions[project_name] = session
        if self._state_changed is not None:
            self._state_changed()
        return session

    async def end(self, project_name: str) -> bool:
        """라이브 세션을 즉시 종료한다. 끝낼 것이 있었으면 True.

        유예 타이머를 먼저 끊는다 — 소켓이 이미 떨어진 세션이면 그쪽도 곧
        terminate를 부르고, 둘이 겹치면 죽은 pid를 두 번 거두게 된다.
        """
        async with self.transition(project_name):
            session = self.get_live(project_name)
            if session is None:
                return False
            session.cancel_grace()
            await session.end_now()
            if self.sessions.get(project_name) is session:
                del self.sessions[project_name]
            return True

    async def _expire_grace(self, project_name: str, session: Session) -> None:
        """grace 만료와 reconnect/end/new를 같은 키 lock에서 선형화한다."""
        async with self.transition(project_name):
            if self.sessions.get(project_name) is not session:
                return
            if session.websocket is not None or not session.alive:
                return
            await session.terminate()
            if self.sessions.get(project_name) is session:
                del self.sessions[project_name]

    async def _expire_idle(self, project_name: str, session: Session) -> None:
        """idle 종료도 attach/new/end와 같은 키 lock에서 선형화한다."""
        async with self.transition(project_name):
            if self.sessions.get(project_name) is not session or not session.alive:
                return
            await session._expire_idle()
            if self.sessions.get(project_name) is session:
                del self.sessions[project_name]

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
        for s in sessions:
            ws = s.websocket
            s.websocket = None
            if ws is not None:
                await s._stop_outbound_writer(ws)
        self.sessions.clear()
