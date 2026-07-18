#!/usr/bin/env bash
# W-Term 서버 백그라운드 실행 (host/port는 projects.json에서 읽음)
cd "$(dirname "$0")"

PID_FILE="logs/wterm.pid"
mkdir -p logs

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "이미 실행 중 (pid $(cat "$PID_FILE"))"
  exit 0
fi

nohup .venv/bin/python -m server >> logs/wterm.out 2>&1 &
echo $! > "$PID_FILE"
echo "백그라운드 기동됨 (pid $!) — 로그: logs/wterm.out"
