#!/usr/bin/env bash
# W-Term 서버 백그라운드 실행 (host/port는 projects.json에서 읽음)
#
# pid 파일은 서버가 직접 쓴다. launchd로 띄울 때도 같은 파일이 나와야
# stop.sh와 인증서 --reloadcmd가 동일하게 동작하기 때문이다.
cd "$(dirname "$0")"

PID_FILE="logs/wterm.pid"
mkdir -p logs

if [ -f "$PID_FILE" ] && pid="$(cat "$PID_FILE" 2>/dev/null)" && [ -n "$pid" ] \
   && kill -0 "$pid" 2>/dev/null; then
  echo "이미 실행 중 (pid $pid)"
  exit 0
fi
rm -f "$PID_FILE"

nohup .venv/bin/python -m server >> logs/wterm.out 2>&1 &
child=$!

# 서버가 pid 파일을 쓸 때까지 대기. 설정 오류(인증서 경로 등)로 즉시 죽는 경우를
# 여기서 잡아준다 — 예전처럼 $!를 그냥 기록하면 죽은 pid가 남는다.
for _ in $(seq 1 100); do
  if [ -f "$PID_FILE" ]; then
    echo "백그라운드 기동됨 (pid $(cat "$PID_FILE")) — 로그: logs/wterm.out"
    exit 0
  fi
  if ! kill -0 "$child" 2>/dev/null; then
    echo "기동 실패 — logs/wterm.out 마지막 20줄:" >&2
    tail -n 20 logs/wterm.out >&2
    exit 1
  fi
  sleep 0.1
done

echo "기동 확인 실패: 10초 안에 pid 파일이 생기지 않음 — logs/wterm.out 확인" >&2
exit 1
