#!/usr/bin/env bash
# W-Term을 macOS 부팅 시 자동 기동시키고, 인증서 갱신을 launchd로 옮긴다.
#
#   sudo ./scripts/install-launchd.sh              설치
#   sudo ./scripts/install-launchd.sh --uninstall   제거
#
# LaunchAgent가 아니라 LaunchDaemon + UserName을 쓰는 이유:
#   LaunchAgent는 "로그인 시"에만 뜬다. 재부팅 후 물리적으로 로그인해야 서버가
#   올라온다면 원격 접속 용도로는 쓸모가 없다. LaunchDaemon은 부팅 시 뜨고,
#   UserName 지정으로 root가 아닌 해당 사용자 권한으로 실행된다.
#
# 갱신을 cron이 아니라 launchd로 돌리는 이유:
#   macOS의 cron은 Mac이 잠들어 있는 동안 실행되지 않고, 깨어나도 놓친 작업을
#   따라잡지 않는다. 늘 잠들어 있는 시각에 잡이 걸리면 갱신이 영영 돌지 않는다.
#   launchd의 StartCalendarInterval은 깨어날 때 놓친 작업을 실행한다.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_LABEL="com.wterm.server"
RENEW_LABEL="com.wterm.certrenew"
DAEMON_DIR="/Library/LaunchDaemons"

if [ "$(id -u)" -ne 0 ]; then
    echo "오류: sudo로 실행하세요 — sudo $0 $*" >&2
    exit 1
fi

# sudo 아래에서 실제 사용자를 알아낸다. 이 사용자 권한으로 데몬이 돈다.
RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    echo "오류: 일반 사용자 계정에서 sudo로 실행해야 합니다 (SUDO_USER 필요)." >&2
    exit 1
fi
RUN_HOME="$(eval echo "~$RUN_USER")"

unload() {
    local label="$1"
    launchctl bootout "system/$label" 2>/dev/null \
        || launchctl unload "$DAEMON_DIR/$label.plist" 2>/dev/null \
        || true
}

if [ "${1:-}" = "--uninstall" ]; then
    for label in "$SERVER_LABEL" "$RENEW_LABEL"; do
        unload "$label"
        rm -f "$DAEMON_DIR/$label.plist"
        echo "제거됨: $label"
    done
    echo
    echo "서버는 아직 떠 있을 수 있습니다. 완전히 내리려면: $REPO_DIR/stop.sh"
    exit 0
fi

# ── 서버 데몬 ────────────────────────────────────────────────────────
# LaunchDaemon은 로그인 셸을 거치지 않아 PATH가 최소값(/usr/bin:/bin:...)이다.
# 서버가 claude/codex를 직접 spawn하므로 이들이 있는 경로를 명시해야 한다.
DAEMON_PATH="$RUN_HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cat > "$DAEMON_DIR/$SERVER_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$SERVER_LABEL</string>
    <key>UserName</key>
    <string>$RUN_USER</string>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>ProgramArguments</key>
    <array>
        <string>$REPO_DIR/.venv/bin/python</string>
        <string>-m</string>
        <string>server</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$DAEMON_PATH</string>
        <key>HOME</key>
        <string>$RUN_HOME</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <!-- 비정상 종료일 때만 재기동. stop.sh(SIGTERM → 정상 종료)로 내린 서버를
         launchd가 곧바로 되살리지 않게 하려는 것. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$REPO_DIR/logs/wterm.out</string>
    <key>StandardErrorPath</key>
    <string>$REPO_DIR/logs/wterm.out</string>
</dict>
</plist>
EOF

chown root:wheel "$DAEMON_DIR/$SERVER_LABEL.plist"
chmod 644 "$DAEMON_DIR/$SERVER_LABEL.plist"

# ── 인증서 갱신 데몬 (acme.sh가 있을 때만) ───────────────────────────
# Homebrew로 설치하면 실행 파일은 /opt/homebrew/bin에 있고 ~/.acme.sh는 데이터
# 디렉터리일 뿐이다. 홈 경로만 보면 갱신 데몬을 조용히 건너뛰게 되므로,
# cert-setup.sh와 같은 방식으로 PATH부터 확인한다.
find_acme() {
    local p
    p="$(sudo -u "$RUN_USER" bash -lc 'command -v acme.sh' 2>/dev/null)"
    if [ -n "$p" ] && [ -x "$p" ]; then echo "$p"; return 0; fi
    for p in "$RUN_HOME/.acme.sh/acme.sh" /opt/homebrew/bin/acme.sh /usr/local/bin/acme.sh; do
        if [ -x "$p" ]; then echo "$p"; return 0; fi
    done
    return 1
}
ACME="$(find_acme || true)"
if [ -n "$ACME" ]; then
    echo "==> acme.sh: $ACME"
    # acme.sh가 등록한 crontab 항목을 제거한다. 남겨두면 launchd와 이중으로 돈다.
    sudo -u "$RUN_USER" "$ACME" --uninstall-cronjob >/dev/null 2>&1 || true

    cat > "$DAEMON_DIR/$RENEW_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$RENEW_LABEL</string>
    <key>UserName</key>
    <string>$RUN_USER</string>
    <key>ProgramArguments</key>
    <array>
        <string>$ACME</string>
        <string>--cron</string>
        <string>--home</string>
        <string>$RUN_HOME/.acme.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$DAEMON_PATH</string>
        <key>HOME</key>
        <string>$RUN_HOME</string>
        <!-- acme.sh는 date로 인증서 만료일을 파싱하는데, 한글 로케일에서는
             영문 월 이름("Nov")을 읽지 못해 갱신 시각 계산의 안전장치가
             조용히 꺼진다. C 로케일로 고정한다. -->
        <key>LC_ALL</key>
        <string>C</string>
    </dict>
    <!-- 매일 03:27. 이 시각에 Mac이 잠들어 있었으면 깨어날 때 실행된다. -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>3</integer>
        <key>Minute</key><integer>27</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$REPO_DIR/logs/cert-renew.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_DIR/logs/cert-renew.log</string>
</dict>
</plist>
EOF
    chown root:wheel "$DAEMON_DIR/$RENEW_LABEL.plist"
    chmod 644 "$DAEMON_DIR/$RENEW_LABEL.plist"
    RENEW_INSTALLED=1
else
    RENEW_INSTALLED=0
fi

# ── 로드 ─────────────────────────────────────────────────────────────
# 기존에 수동으로 띄운 서버가 있으면 포트가 겹치므로 먼저 내린다.
if [ -f "$REPO_DIR/logs/wterm.pid" ]; then
    echo "==> 실행 중인 서버를 먼저 내립니다."
    sudo -u "$RUN_USER" "$REPO_DIR/stop.sh" || true
fi

for label in "$SERVER_LABEL" $([ "$RENEW_INSTALLED" = 1 ] && echo "$RENEW_LABEL"); do
    unload "$label"
    launchctl bootstrap system "$DAEMON_DIR/$label.plist" \
        || launchctl load "$DAEMON_DIR/$label.plist"
    echo "설치 및 로드됨: $label"
done

if [ "$RENEW_INSTALLED" = 0 ]; then
    # 조용히 넘어가면 만료일까지 아무도 모른다. 눈에 띄게 남긴다.
    cat >&2 <<EOF

########################################################################
경고: acme.sh를 찾지 못해 인증서 갱신 데몬을 설치하지 못했습니다.
      이대로 두면 인증서 만료 시 접속이 끊깁니다.
      ./scripts/cert-setup.sh 로 발급을 마친 뒤 이 스크립트를 다시 실행하세요.
      확인: ./scripts/cert-status.sh
########################################################################
EOF
fi

cat <<EOF

==> 완료. 이제 부팅 시 자동으로 기동합니다 (로그인 불필요).

  상태 확인   sudo launchctl print system/$SERVER_LABEL | head -20
  재시작      sudo launchctl kickstart -k system/$SERVER_LABEL
  중지        sudo launchctl bootout system/$SERVER_LABEL
  로그        $REPO_DIR/logs/wterm.out
  제거        sudo $0 --uninstall

주의: 코드를 수정한 뒤에는 kickstart로 재시작해야 반영됩니다 (reload 미사용).
EOF
