#!/usr/bin/env bash
# W-Term 서버 종료.
# cwd가 이 저장소인 ".venv/bin/python -m server" 프로세스만 골라 SIGTERM을 보낸다.
# 서버는 종료 시 SessionManager.shutdown()으로 자식 claude 세션을 직접 회수하므로
# 여기서는 서버 pid에만 시그널을 보내면 된다 (pgrep claude 금지 — CLAUDE.md 참조).
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd -P)"

pids=()
for pid in $(pgrep -f '\.venv/bin/python -m server' || true); do
    if [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" = "$HERE" ]; then
        pids+=("$pid")
    fi
done

if [ ${#pids[@]} -eq 0 ]; then
    echo "W-Term: 실행 중인 서버가 없습니다."
    exit 0
fi

echo "W-Term: SIGTERM → pid ${pids[*]}"
kill -TERM "${pids[@]}"

# 세션 정리(SIGTERM 10초 대기 + SIGKILL)까지 감안해 최대 20초 대기
for _ in $(seq 1 200); do
    alive=0
    for pid in "${pids[@]}"; do
        kill -0 "$pid" 2>/dev/null && alive=1
    done
    [ "$alive" -eq 0 ] && { echo "W-Term: 종료 완료."; exit 0; }
    sleep 0.1
done

echo "W-Term: 종료되지 않아 SIGKILL을 보냅니다."
kill -KILL "${pids[@]}" 2>/dev/null || true
echo "W-Term: 강제 종료 완료."
