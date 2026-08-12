"""세션 내부 배압/프레임 순서처럼 실제 소켓 밖에서도 고정해야 하는 경계."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from server import session as session_mod
from server.session import (
    OUTPUT_HIGH_WATERMARK,
    OUTPUT_LOW_WATERMARK,
    READ_CHUNK,
    HistoryState,
    WRITE_BUFFER_LIMIT,
    Session,
    SessionManager,
    remote_has_history,
)


class BlockingWebSocket:
    """send_bytes를 막아 읽지 않는 브라우저를 결정적으로 재현한다."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.events: list[tuple[str, object]] = []

    async def send_bytes(self, data: bytes) -> None:
        self.started.set()
        await self.release.wait()
        self.events.append(("bytes", data))

    async def send_text(self, data: str) -> None:
        self.events.append(("text", json.loads(data)))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.events.append(("close", (code, reason)))


def _session() -> Session:
    session = Session("demo#shell", "/tmp", 60, agent="shell")
    session.alive = True
    session.master_fd = 123
    return session


def test_write_buffer_admission_is_a_real_hard_limit(monkeypatch):
    async def scenario():
        session = _session()
        session._writer_registered = True  # 가짜 fd를 loop에 등록하지 않게 한다.
        monkeypatch.setattr(session_mod.os, "write", lambda *_: (_ for _ in ()).throw(
            BlockingIOError()
        ))

        session._write_buffer.extend(b"A" * (WRITE_BUFFER_LIMIT - 1))
        assert session.write_input("B") is False
        assert len(session._write_buffer) == WRITE_BUFFER_LIMIT  # 정확한 경계는 허용

        session._write_buffer[:] = b"A" * (WRITE_BUFFER_LIMIT - 2)
        session._input_dropped = False
        assert session.write_input("한") is True  # UTF-8 3바이트가 남은 2바이트를 넘음
        assert session._write_buffer == b"A" * (WRITE_BUFFER_LIMIT - 2)

        session._write_buffer.clear()
        session._input_dropped = False
        assert session.write_input("Z" * (WRITE_BUFFER_LIMIT + 1)) is True
        assert not session._write_buffer  # 큰 단일 프레임도 한 번은 받는 예외가 없다

    asyncio.run(scenario())


def test_partial_write_and_eagain_keep_exact_utf8_suffix(monkeypatch):
    async def scenario():
        session = _session()
        session._writer_registered = True
        calls = 0

        def partial_then_block(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return 1
            raise BlockingIOError

        monkeypatch.setattr(session_mod.os, "write", partial_then_block)
        assert session.write_input("한") is False
        assert bytes(session._write_buffer) == "한".encode()[1:]

        # fd가 다시 writable이 된 뒤 남은 두 바이트가 그대로 빠진다.
        session._writer_registered = False
        monkeypatch.setattr(session_mod.os, "write", lambda fd, data: len(data))
        session._flush_write()
        assert not session._write_buffer

    asyncio.run(scenario())


def test_slow_client_caps_pending_output_and_uses_one_writer(monkeypatch):
    async def scenario():
        session = _session()
        ws = BlockingWebSocket()
        paused = 0
        resumed = 0

        def pause():
            nonlocal paused
            paused += 1
            session._pty_reader_paused = True

        def resume():
            nonlocal resumed
            if session._pty_reader_paused:
                resumed += 1
                session._pty_reader_paused = False

        monkeypatch.setattr(session, "_pause_pty_reader", pause)
        monkeypatch.setattr(session, "_resume_pty_reader", resume)
        monkeypatch.setattr(session_mod.os, "read", lambda *_: b"X" * READ_CHUNK)

        await session.attach(ws)
        while not session._pty_reader_paused:
            session._on_pty_readable()  # 대량 출력 자식을 PTY callback 수준에서 재현

        assert paused == 1
        assert OUTPUT_HIGH_WATERMARK <= session._out_pending_bytes
        assert session._out_pending_bytes <= OUTPUT_HIGH_WATERMARK + READ_CHUNK
        writers = [
            task for task in asyncio.all_tasks()
            if task.get_name() == "wterm-output:demo#shell" and not task.done()
        ]
        assert len(writers) == 1

        await ws.started.wait()
        ws.release.set()
        for _ in range(100):
            if session._out_pending_bytes <= OUTPUT_LOW_WATERMARK:
                break
            await asyncio.sleep(0)
        assert session._out_pending_bytes <= OUTPUT_LOW_WATERMARK
        assert resumed == 1
        await session.detach(ws)
        assert session._out_writer_task is None
        assert session._out_pending_bytes == 0

    asyncio.run(scenario())


def test_replay_live_status_and_final_output_exit_close_are_serialized(monkeypatch):
    async def scenario():
        session = _session()
        session.buffer.extend(b"replay")
        monkeypatch.setattr(session_mod.os, "read", lambda *_: b"live")
        # 가짜 fd에서 실제 add/remove_reader를 부르지 않는다.
        monkeypatch.setattr(session, "_pause_pty_reader", lambda: None)
        monkeypatch.setattr(session, "_resume_pty_reader", lambda: None)
        ws = BlockingWebSocket()

        attach = asyncio.create_task(session.attach(ws))
        await ws.started.wait()  # replay send가 막힌 동안 live output이 도착한다.
        session._on_pty_readable()
        ws.release.set()
        await attach
        session.send_status("ready")
        while len(ws.events) < 3:
            await asyncio.sleep(0)
        assert ws.events[:3] == [
            ("bytes", b"replay"),
            ("bytes", b"live"),
            ("text", {"type": "status", "message": "ready"}),
        ]

        # 자연 종료 시 이미 큐에 들어간 마지막 output 뒤에 exit와 close가 붙는다.
        session._enqueue_outbound(session_mod._Outbound("bytes", b"final"))
        monkeypatch.setattr(session, "_cleanup_fd", lambda: None)
        monkeypatch.setattr(session, "_reap", lambda: 7)
        await session._handle_exit()
        assert ws.events[-3:] == [
            ("bytes", b"final"),
            ("text", {"type": "exit", "code": 7}),
            ("close", (1000, "세션이 종료됨")),
        ]
        assert session._out_writer_task is None

    asyncio.run(scenario())


def _fake_ssh(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ssh"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_remote_history_distinguishes_present_absent_and_failures(tmp_path, monkeypatch):
    async def run_with(body: str) -> HistoryState:
        _fake_ssh(tmp_path, body)
        return await remote_has_history("example", "/work/demo")

    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert asyncio.run(run_with("exit 0")) is HistoryState.PRESENT
    assert asyncio.run(run_with("exit 1")) is HistoryState.ABSENT
    assert asyncio.run(run_with("exit 2")) is HistoryState.ERROR
    assert asyncio.run(run_with("exit 255")) is HistoryState.ERROR


def test_remote_history_reports_missing_ssh_binary_as_error(monkeypatch):
    async def missing(*args, **kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    assert asyncio.run(remote_has_history("host", "/work/demo")) is HistoryState.ERROR


def test_remote_history_uses_noninteractive_host_key_policy(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv"
    _fake_ssh(tmp_path, f'printf "%s\\n" "$@" > {argv_file}; exit 255')
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    assert asyncio.run(remote_has_history("new-host", "/work/demo")) is HistoryState.ERROR
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "ConnectTimeout=5" in argv


def test_remote_history_timeout_reaps_the_subprocess(tmp_path, monkeypatch):
    marker = tmp_path / "terminated"
    ready = tmp_path / "ready"
    script = tmp_path / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, signal, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "def stop(*_):\n"
        "    marker.write_text('terminated')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(session_mod, "REMOTE_HISTORY_TIMEOUT", 1.0)

    assert asyncio.run(remote_has_history("slow", "/work/demo")) is HistoryState.ERROR
    assert marker.read_text(encoding="utf-8") == "terminated"


def test_remote_history_cancellation_reaps_the_subprocess(tmp_path, monkeypatch):
    marker = tmp_path / "cancelled"
    ready = tmp_path / "ready"
    script = tmp_path / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, signal, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "def stop(*_):\n"
        "    marker.write_text('cancelled')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    async def scenario():
        task = asyncio.create_task(remote_has_history("slow", "/work/demo"))
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(0.02)
        assert ready.exists()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert marker.read_text(encoding="utf-8") == "cancelled"


def test_session_transition_lock_is_per_key_not_global():
    async def scenario():
        manager = SessionManager(60)
        slow_entered = asyncio.Event()
        release_slow = asyncio.Event()
        other_finished = asyncio.Event()

        async def slow_history_transition():
            async with manager.transition("slow#claude"):
                slow_entered.set()
                await release_slow.wait()

        async def other_project_transition():
            await slow_entered.wait()
            async with manager.transition("other#shell"):
                other_finished.set()

        slow = asyncio.create_task(slow_history_transition())
        other = asyncio.create_task(other_project_transition())
        await asyncio.wait_for(other_finished.wait(), 1)
        assert not slow.done()
        release_slow.set()
        await asyncio.gather(slow, other)

    asyncio.run(scenario())
