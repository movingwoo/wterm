#!/usr/bin/env bash
# W-Term 서버 기동 (host/port는 projects.json에서 읽음)
#
# pid 파일은 서버가 직접 쓴다. launchd로 띄울 때도 같은 파일이 나와야
# stop.sh와 인증서 --reloadcmd가 동일하게 동작하기 때문이다.
#
# 감시자(launchd/systemd)가 설치돼 있으면 직접 띄우지 않고 감시자를 통해 띄운다.
# 직접 띄우면 감시자 밖의 프로세스가 생겨서, 크래시해도 아무도 되살리지 않고
# 환경(PATH/HOME/LANG)도 유닛에 적힌 것이 아니라 호출한 셸의 것을 물려받는다.
# 데몬은 로그인 셸을 거치지 않아 PATH가 최소값이라, 여기서 갈리면 claude/codex
# 해석 경로가 "손으로 띄웠을 때만 되는" 상태가 된다.
cd "$(dirname "$0")"

PID_FILE="logs/wterm.pid"
LAUNCHD_LABEL="com.wterm.server"
LAUNCHD_PLIST="/Library/LaunchDaemons/$LAUNCHD_LABEL.plist"
SYSTEMD_UNIT="wterm.service"
mkdir -p logs

if [ -f "$PID_FILE" ] && pid="$(cat "$PID_FILE" 2>/dev/null)" && [ -n "$pid" ] \
   && kill -0 "$pid" 2>/dev/null; then
  echo "이미 실행 중 (pid $pid)"
  exit 0
fi
rm -f "$PID_FILE"

# 서버가 pid 파일을 쓸 때까지 대기. 설정 오류(인증서 경로 등)로 즉시 죽는 경우를
# 여기서 잡아준다 — 예전처럼 $!를 그냥 기록하면 죽은 pid가 남는다.
# $1이 있으면 그 pid가 살아있는지도 같이 본다(직접 기동한 경우).
wait_for_pid_file() {
  local child="${1:-}"
  for _ in $(seq 1 100); do
    if [ -f "$PID_FILE" ]; then
      echo "기동됨 (pid $(cat "$PID_FILE")) — 로그: logs/wterm.out"
      return 0
    fi
    if [ -n "$child" ] && ! kill -0 "$child" 2>/dev/null; then
      echo "기동 실패 — logs/wterm.out 마지막 20줄:" >&2
      tail -n 20 logs/wterm.out >&2
      return 1
    fi
    sleep 0.1
  done
  echo "기동 확인 실패: 10초 안에 pid 파일이 생기지 않음 — logs/wterm.out 확인" >&2
  return 1
}

# 감시자를 통한 기동. 시스템 도메인이라 root가 필요하다.
# 비밀번호 프롬프트 없이(-n) 되는지 먼저 보고, 안 되면 조용히 직접 기동으로
# 빠지지 않고 명령을 안내하고 멈춘다 — 조용한 폴백은 "감시자 밖 프로세스"라는
# 지금 막으려는 상황을 그대로 다시 만든다.
supervisor_start() {
  local what="$1" cmd="$2"
  if [ "$(id -u)" -eq 0 ]; then
    eval "$cmd" || return 1
  elif sudo -n true 2>/dev/null; then
    eval "sudo $cmd" || return 1
  else
    echo "$what(으)로 관리되는 서버입니다. 기동에 root 권한이 필요합니다:" >&2
    echo "  sudo $cmd" >&2
    echo "  (또는 sudo $0)" >&2
    return 2
  fi
  wait_for_pid_file
}

if [ -f "$LAUNCHD_PLIST" ]; then
  # 이 시점에는 서버가 떠 있지 않은 것이 확인된 상태라 kickstart의 -k(실행 중인
  # 인스턴스를 죽인 뒤 재기동)는 필요 없다. -k는 stop.sh의 SIGTERM 경로를 타지
  # 않아 PTY 세션 회수(SessionManager.shutdown)를 건너뛴다 — 세션은 setsid로
  # 자기 프로세스 그룹에 있어서 서버만 사라지고 살아남는다.
  supervisor_start "launchd" "launchctl kickstart system/$LAUNCHD_LABEL"
  rc=$?
  # 1 = 명령이 실제로 실패. 2 = 권한이 없어 시도조차 못 함(안내는 이미 끝났다).
  if [ $rc -eq 1 ]; then
    echo "  플리스트는 있는데 잡이 로드돼 있지 않다면:" >&2
    echo "    sudo launchctl bootstrap system $LAUNCHD_PLIST" >&2
  fi
  [ $rc -eq 0 ] || exit 1
elif command -v systemctl >/dev/null 2>&1 && systemctl cat "$SYSTEMD_UNIT" >/dev/null 2>&1; then
  supervisor_start "systemd" "systemctl start $SYSTEMD_UNIT" || exit 1
else
  # 감시자 없음 — 직접 띄운다. root로 띄우면 여기서 spawn되는 claude/codex/셸이
  # 전부 root가 되므로 막는다(감시자 유닛은 UserName/User로 강등해서 돈다).
  if [ "$(id -u)" -eq 0 ]; then
    echo "오류: 감시자 없이 root로 띄우면 Claude/Codex/셸 세션이 전부 root가 됩니다." >&2
    echo "      일반 사용자로 실행하세요: $0" >&2
    exit 1
  fi
  nohup .venv/bin/python -m server >> logs/wterm.out 2>&1 &
  wait_for_pid_file "$!"
fi
