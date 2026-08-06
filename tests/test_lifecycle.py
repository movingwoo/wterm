"""프로세스 수명 주기 — pid 파일 잠금과 종료 코드.

AGENTS.md "Process lifecycle"의 두 가지를 지킨다:

- pid 파일은 "서버는 하나"라는 잠금이다. 두 번째 서버는 pid를 덮어쓰는 대신
  기동을 거부해야 한다. 덮어쓰던 시절에는 첫 번째 서버가 stop.sh와 인증서
  리로드 양쪽에서 영영 닿지 않는 상태가 됐다.
- SIGTERM 종료는 **종료 코드 0**이어야 한다. 시그널로 죽으면 감시자가 크래시로
  보고 stop.sh로 내린 서버를 즉시 되살린다.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

# server/main.py의 EXIT_ALREADY_RUNNING. 상수를 import하려면 server.main을 읽어야
# 하는데, 그러면 저장소 루트의 실제 projects.json을 로드하게 된다(CI에는 없다).
EXIT_ALREADY_RUNNING = 3


def test_pid_file_holds_server_pid(start_server):
    h = start_server()
    assert h.pid_file.read_text().strip() == str(h.proc.pid)


def test_sigterm_exits_zero_and_clears_pid_file(start_server):
    """감시자가 '정상 종료'로 읽어야 stop.sh가 되돌려지지 않는다."""
    h = start_server()
    assert h.stop(signal.SIGTERM) == 0
    assert not h.pid_file.exists()


def test_second_instance_is_refused(start_server):
    """포트가 달라도 막아야 한다 — 바인딩 충돌은 이걸 잡아주지 않는다."""
    first = start_server()
    original = first.pid_file.read_text()

    second = start_server(wait=False)  # 같은 저장소 사본 = 같은 pid 파일, 다른 포트
    assert second.proc.wait(timeout=30) == EXIT_ALREADY_RUNNING
    assert "이미 서버가 실행 중" in second.output()

    # 거부된 쪽이 pid 파일을 건드리지 않았고, 첫 서버는 멀쩡하다.
    assert first.pid_file.read_text() == original
    assert first.client().get("/").status_code == 200


def test_lock_is_released_when_server_is_killed(start_server):
    """커널이 잠금을 놓아주므로 남은 pid 파일에 대한 stale 판정이 필요 없다."""
    first = start_server()
    first.proc.send_signal(signal.SIGKILL)
    first.proc.wait(timeout=10)
    assert first.pid_file.exists(), "SIGKILL은 정리 훅을 타지 않는다 (전제 확인)"

    second = start_server()  # 남아 있는 pid 파일에도 불구하고 떠야 한다
    assert second.pid_file.read_text().strip() == str(second.proc.pid)


def test_stop_script_stops_the_server(start_server, repo_copy):
    """`./stop.sh`가 pid 파일만으로 서버를 내릴 수 있어야 한다."""
    h = start_server()
    # stop.sh는 `kill -0`으로 종료를 확인한다. 서버는 여기서 pytest의 자식이라
    # 우리가 wait()으로 거둬가기 전까지 좀비로 남고 `kill -0`이 계속 성공한다.
    # 스크립트가 도는 동안 나란히 거둬야 한다 (테스트 구성상의 제약이다 —
    # 실제 운영에서는 서버의 부모가 감시자거나 init이다).
    stop = subprocess.Popen(
        ["bash", "./stop.sh"], cwd=repo_copy,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert h.proc.wait(timeout=30) == 0
    out = stop.communicate(timeout=60)[0]
    assert stop.returncode == 0, out
    assert not h.pid_file.exists()


def test_shutdown_reaps_pty_children(start_server, project_dir):
    """서버가 죽으면 자식 셸도 같이 정리된다 (setsid라 그냥 두면 살아남는다)."""
    from test_ws import send_line, status_message, ws_connect

    pid_marker = project_dir / "shell.pid"  # 셸의 cwd는 프로젝트 경로다
    pid_marker.unlink(missing_ok=True)

    h = start_server()
    token = h.login()
    with ws_connect(h, "/ws/demo?agent=shell", token=token) as ws:
        status_message(ws)
        send_line(ws, f"echo $$ > {pid_marker.name}")
        child_pid = _read_pid(pid_marker)
        os.kill(child_pid, 0)  # 살아 있다 (아니면 ProcessLookupError)

    assert h.stop(signal.SIGTERM) == 0

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(f"셸 자식 프로세스 {child_pid}가 서버보다 오래 살아남았다")


def _read_pid(path, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError(f"셸이 {timeout}초 안에 {path}를 쓰지 않았다")
