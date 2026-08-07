#!/usr/bin/env bash
# W-Term 서버 종료.
# 서버가 기록한 logs/wterm.pid를 사용해 이 저장소에서 띄운 서버 프로세스만 종료한다.
# (/proc/$pid/cwd 기반 조회는 macOS에 /proc이 없어 항상 실패하므로 사용하지 않는다)
# 서버는 종료 시 SessionManager.shutdown()으로 자식 claude 세션을 직접 회수하므로
# 여기서는 서버 pid에만 시그널을 보내면 된다 (pgrep claude 금지 — AGENTS.md 참조).
#
# 감시자(launchd/systemd)와의 관계: 서버는 SIGTERM을 받으면 종료 코드 0으로 끝나고
# 감시자는 "정상 종료"로 보아 되살리지 않는다(server/main.py의 _exit_success 참조).
# 하지만 SIGKILL로 죽이면 비정상 종료로 보여 곧바로 되살아난다. 그래서 여기서는
# 내린 뒤 실제로 내려간 상태가 유지되는지까지 확인한다.
set -euo pipefail
cd "$(dirname "$0")"

PID_FILE="logs/wterm.pid"
LAUNCHD_PLIST="/Library/LaunchDaemons/com.wterm.server.plist"

# 감시자가 설치돼 있으면 "완전히 내리는 방법"을 안내하기 위한 힌트 문자열.
supervisor_hint() {
    if [ -f "$LAUNCHD_PLIST" ]; then
        echo "sudo launchctl bootout system/com.wterm.server"
    elif command -v systemctl >/dev/null 2>&1 && systemctl cat wterm.service >/dev/null 2>&1; then
        echo "sudo systemctl stop wterm"
    fi
    return 0  # 감시자가 없을 때 마지막 test의 실패 상태가 새어나가지 않게
}

# 내가 죽인 pid가 아직 들어 있을 때만 지운다. 감시자가 즉시 새 서버를 띄웠다면
# 그쪽이 이미 자기 pid를 써둔 상태이므로 건드리면 안 된다.
drop_pid_file() {
    [ "$(cat "$PID_FILE" 2>/dev/null || true)" = "$1" ] && rm -f "$PID_FILE"
    return 0
}

# 종료 후 감시자가 되살리지 않는지 확인한다. 이미 오래 떠 있던 프로세스라면
# launchd의 ThrottleInterval(10초)이 이미 충족돼 재기동이 거의 즉시 일어난다.
verify_down() {
    local old_pid="$1" hint new_pid
    for _ in $(seq 1 30); do
        sleep 0.1
        [ -f "$PID_FILE" ] || continue
        new_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [ -n "$new_pid" ] && [ "$new_pid" != "$old_pid" ] && kill -0 "$new_pid" 2>/dev/null; then
            hint="$(supervisor_hint)"
            echo "W-Term: 감시자가 서버를 다시 띄웠습니다 (새 pid $new_pid)." >&2
            if [ -n "$hint" ]; then
                echo "        완전히 내리려면: $hint" >&2
            fi
            return 1
        fi
    done
    return 0
}

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
        drop_pid_file "$pid"
        verify_down "$pid" || exit 1
        exit 0
    fi
    sleep 0.1
done

echo "W-Term: 종료되지 않아 SIGKILL을 보냅니다." >&2
kill -KILL "$pid" 2>/dev/null || true
drop_pid_file "$pid"
echo "W-Term: 강제 종료 완료." >&2

# SIGKILL은 감시자에게 '비정상 종료'로 보인다 — 되살아나는 게 정상 동작이다.
hint="$(supervisor_hint)"
if [ -n "$hint" ]; then
    echo "        SIGKILL은 감시자에게 비정상 종료로 보여 곧 다시 뜹니다." >&2
    echo "        완전히 내리려면: $hint" >&2
fi
exit 1
