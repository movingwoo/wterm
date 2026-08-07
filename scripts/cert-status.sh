#!/usr/bin/env bash
# W-Term TLS 인증서 및 비밀 파일 권한 점검.
#
# 이 구조의 위험은 "조용한 실패"다. 갱신이 멈춰도 만료일까지 아무 증상이 없고,
# 갱신은 됐는데 SIGHUP이 안 갔으면 파일과 실제 서빙 인증서가 어긋난 채로 돈다.
# 파일 권한도 마찬가지로 틀려 있어도 아무 증상이 없다 — 넓게 열린 채 몇 달을
# 돈다. 셋 다 눈으로는 안 보이므로 이 스크립트로 확인한다.
#
#   ./scripts/cert-status.sh
#
# 이상이 있으면 종료 코드 1.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO_DIR/.venv/bin/python"
PID_FILE="$REPO_DIR/logs/wterm.pid"
PROBLEMS=0

# printf의 %-Ns는 바이트로 패딩해서 한글 라벨이 어긋난다. 표시 폭(한글 2칸)으로 직접 맞춘다.
col() {
    local lbl="$1" w=0 i c
    for (( i = 0; i < ${#lbl}; i++ )); do
        c="${lbl:i:1}"
        if [[ "$c" == [[:ascii:]] ]]; then w=$((w + 1)); else w=$((w + 2)); fi
    done
    local gap=$((18 - w))
    [ "$gap" -lt 1 ] && gap=1
    printf '%s%*s' "$lbl" "$gap" ""
}
say()  { printf '%s %s\n' "$(col "$1")" "$2"; }
bad()  { printf '%s ⚠️  %s\n' "$(col "$1")" "$2"; PROBLEMS=$((PROBLEMS + 1)); }

# ── 설정 읽기 ────────────────────────────────────────────────────────
CFG_VARS="$("$PY" - "$REPO_DIR/projects.json" <<'EOF'
import json, shlex, sys
from pathlib import Path
raw = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for key in ("tls_certfile", "tls_keyfile", "host", "port"):
    print(f"CFG_{key.upper()}={shlex.quote(str(raw.get(key) or ''))}")
print(f"CFG_HAS_PASSWORD_HASH={'1' if raw.get('password_hash') else ''}")
EOF
)" || { echo "projects.json을 읽을 수 없습니다." >&2; exit 1; }
eval "$CFG_VARS"

# 권한은 macOS(BSD)와 리눅스(GNU)의 stat 문법이 달라 둘 다 시도한다.
file_mode() {
    stat -f "%OLp" "$1" 2>/dev/null || stat -c "%a" "$1" 2>/dev/null
}

# group/other 비트가 하나라도 서 있으면 지적한다. 정확히 600일 필요는 없고
# (700인 디렉터리 등) 본인 외에 읽히지 않는 것이 요건이다.
check_private() {
    local label="$1" path="$2" why="$3" mode
    [ -e "$path" ] || { bad "$label" "파일이 없습니다: $path"; return; }
    mode="$(file_mode "$path")"
    if [ -z "$mode" ]; then
        bad "$label" "권한을 읽지 못했습니다: $path"
    elif [ "${mode: -2}" = "00" ]; then
        say "$label" "$mode"
    else
        bad "$label" "$mode — $why"
        echo "                       해결: chmod 600 '$path'"
    fi
}

echo "=== W-Term 상태 점검 ==="
echo

# ── 비밀 파일 권한 ───────────────────────────────────────────────────
# TLS 설정 여부와 무관하게 먼저 본다. password_hash는 오프라인 크래킹 대상이라
# 해시라는 이유로 공개돼도 되는 값이 아니다.
if [ -n "${CFG_HAS_PASSWORD_HASH:-}" ]; then
    check_private "projects.json" "$REPO_DIR/projects.json" \
        "패스워드 해시가 들어 있어 다른 사용자에게 읽히면 안 됩니다"
fi

if [ -z "${CFG_TLS_CERTFILE:-}" ] || [ -z "${CFG_TLS_KEYFILE:-}" ]; then
    echo
    echo "TLS가 설정되어 있지 않습니다 (projects.json의 tls_certfile/tls_keyfile 없음)."
    [ "$PROBLEMS" -eq 0 ] && exit 0
    echo "확인이 필요한 항목 ${PROBLEMS}건."
    exit 1
fi
echo

say "인증서 파일" "$CFG_TLS_CERTFILE"

if [ ! -r "$CFG_TLS_CERTFILE" ]; then
    bad "파일 존재" "읽을 수 없음"
    exit 1
fi

# ── 만료일 ───────────────────────────────────────────────────────────
NOT_AFTER="$(openssl x509 -in "$CFG_TLS_CERTFILE" -noout -enddate 2>/dev/null | cut -d= -f2)"
SUBJECT="$(openssl x509 -in "$CFG_TLS_CERTFILE" -noout -subject 2>/dev/null | sed 's/^subject= *//')"
ISSUER="$(openssl x509 -in "$CFG_TLS_CERTFILE" -noout -issuer 2>/dev/null | sed 's/^issuer= *//')"
say "주체" "$SUBJECT"
say "발급자" "$ISSUER"

# LC_ALL=C 필수: 한글 로케일에서는 date가 "Nov" 같은 영문 월 이름을 파싱하지 못한다.
END_EPOCH="$(LC_ALL=C date -j -f "%b %e %T %Y %Z" "$NOT_AFTER" "+%s" 2>/dev/null \
             || LC_ALL=C date -d "$NOT_AFTER" "+%s" 2>/dev/null)"
if [ -n "$END_EPOCH" ]; then
    DAYS=$(( (END_EPOCH - $(date "+%s")) / 86400 ))
    if [ "$DAYS" -lt 0 ]; then
        bad "만료" "$NOT_AFTER (이미 만료됨)"
    elif [ "$DAYS" -lt 20 ]; then
        bad "만료" "$NOT_AFTER (${DAYS}일 남음 — 갱신이 멈췄을 수 있음)"
    else
        say "만료" "$NOT_AFTER (${DAYS}일 남음)"
    fi
else
    say "만료" "$NOT_AFTER"
fi

# ── 키 권한 ──────────────────────────────────────────────────────────
check_private "개인키 권한" "$CFG_TLS_KEYFILE" \
    "개인키가 다른 사용자에게 읽히면 TLS가 무의미해집니다"

# ── 파일 vs 실제 서빙 중인 인증서 ────────────────────────────────────
# 여기가 어긋나면 갱신은 됐는데 SIGHUP이 전달되지 않았다는 뜻이다.
echo
FILE_FP="$(openssl x509 -in "$CFG_TLS_CERTFILE" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    CONNECT_HOST="${CFG_HOST:-127.0.0.1}"
    SERVED_FP="$(openssl s_client -connect "$CONNECT_HOST:${CFG_PORT}" </dev/null 2>/dev/null \
                 | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
    if [ -z "$SERVED_FP" ]; then
        bad "서빙 인증서" "$CONNECT_HOST:$CFG_PORT 에 TLS로 접속하지 못했습니다"
    elif [ "$SERVED_FP" = "$FILE_FP" ]; then
        say "서빙 인증서" "파일과 일치 ✅"
    else
        bad "서빙 인증서" "파일과 불일치 — SIGHUP이 전달되지 않았습니다"
        echo "                       해결: kill -HUP \$(cat $PID_FILE)"
    fi
else
    say "서빙 인증서" "서버가 실행 중이 아니라 확인 생략"
fi

# ── 갱신 잡 등록 여부 ────────────────────────────────────────────────
echo
if [ "$(uname -s)" = "Darwin" ]; then
    INSTALLER="install-launchd.sh"
    if launchctl print system/com.wterm.certrenew >/dev/null 2>&1; then
        say "갱신 잡" "launchd (com.wterm.certrenew) ✅"
        RENEW_JOB=1
    fi
else
    INSTALLER="install-systemd.sh"
    # 유닛이 존재하는 것과 타이머가 실제로 켜져 있는 것은 다르다. enable 여부까지 본다.
    if systemctl is-enabled wterm-certrenew.timer >/dev/null 2>&1; then
        NEXT="$(systemctl list-timers --all wterm-certrenew.timer 2>/dev/null | sed -n 2p)"
        say "갱신 잡" "systemd timer (wterm-certrenew.timer) ✅"
        [ -n "$NEXT" ] && echo "                       다음 실행: $NEXT"
        RENEW_JOB=1
    elif systemctl cat wterm-certrenew.timer >/dev/null 2>&1; then
        bad "갱신 잡" "wterm-certrenew.timer가 설치돼 있지만 enable되지 않았습니다"
        echo "                       해결: sudo systemctl enable --now wterm-certrenew.timer"
        RENEW_JOB=1
    fi
fi

if [ -z "${RENEW_JOB:-}" ]; then
    if crontab -l 2>/dev/null | grep -q "acme.sh"; then
        say "갱신 잡" "cron (acme.sh)"
        if [ "$INSTALLER" = "install-launchd.sh" ]; then
            echo "                       ⓘ  macOS의 cron은 잠자기 중 실행되지 않고 놓친 작업을"
            echo "                          따라잡지 않습니다. sudo ./scripts/$INSTALLER 권장"
        else
            echo "                       ⓘ  cron은 머신이 꺼져 있던 동안 놓친 작업을 따라잡지"
            echo "                          않습니다. sudo ./scripts/$INSTALLER 권장"
        fi
    else
        bad "갱신 잡" "등록된 갱신 잡이 없습니다 — 만료되면 접속이 끊깁니다"
    fi
fi

# ── 마지막 갱신 / 리로드 흔적 ────────────────────────────────────────
CERT_MTIME="$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$CFG_TLS_CERTFILE" 2>/dev/null \
              || stat -c "%y" "$CFG_TLS_CERTFILE" 2>/dev/null)"
say "인증서 갱신 시각" "$CERT_MTIME"

LAST_RELOAD="$(grep "인증서를 다시 읽었습니다" "$REPO_DIR/logs/wterm.log" 2>/dev/null | tail -n 1)"
if [ -n "$LAST_RELOAD" ]; then
    # 로그 레벨 표기는 떼고 시각만 보여준다 (성공 기록이 장애로 읽히지 않게).
    say "마지막 리로드" "$(printf '%s' "$LAST_RELOAD" | cut -d, -f1)"
else
    say "마지막 리로드" "기록 없음 (아직 갱신이 없었다면 정상)"
fi

echo
if [ "$PROBLEMS" -eq 0 ]; then
    echo "이상 없음."
    exit 0
fi
echo "확인이 필요한 항목 ${PROBLEMS}건."
exit 1
