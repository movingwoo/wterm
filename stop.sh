#!/usr/bin/env bash
# W-Term 서버 종료.
# start.sh가 기록한 logs/wterm.pid를 사용해 이 저장소에서 띄운 서버 프로세스만 종료한다.
# (/proc/$pid/cwd 기반 조회는 macOS에 /proc이 없어 항상 실패하므로 사용하지 않는다)
# 서버는 종료 시 SessionManager.shutdown()으로 자식 claude 세션을 직접 회수하므로
# 여기서는 서버 pid에만 시그널을 보내면 된다 (pgrep claude 금지 — CLAUDE.md 참조).
set -euo pipefail
cd "$(dirname "$0")"

PID_FILE="logs/wterm.pid"

if [ ! -f "$PID_FILE" ] || ! pid="$(cat "$PID_FILE")" || [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    echo "W-Term: 실행 중인 서버가 없습니다."
    rm -f "$PID_FILE"
    exit 0
fi

echo "W-Term: SIGTERM → pid $pid"
kill -TERM "$pid"

# 세션 정리(SIGTERM 10초 대기 + SIGKILL)까지 감안해 최대 20초 대기
for _ in $(seq 1 200); do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "W-Term: 종료 완료."
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 0.1
done

echo "W-Term: 종료되지 않아 SIGKILL을 보냅니다."
kill -KILL "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "W-Term: 강제 종료 완료."
