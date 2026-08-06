#!/usr/bin/env bash
# W-Term을 리눅스 부팅 시 자동 기동시키고, 인증서 갱신을 systemd timer로 옮긴다.
# scripts/install-launchd.sh(macOS)의 1:1 대응물이다.
#
#   sudo ./scripts/install-systemd.sh              설치
#   sudo ./scripts/install-systemd.sh --uninstall   제거
#
# 사용자 유닛(systemd --user)이 아니라 시스템 유닛 + User=를 쓰는 이유:
#   사용자 유닛은 그 사용자의 로그인 세션이 있어야 뜬다. loginger(linger)를
#   켜면 부팅 시에도 뜨지만, 설정이 사용자 계정 상태에 숨어 있어 "왜 안 떴는지"를
#   추적하기 어렵다. 시스템 유닛 + User=는 launchd의 LaunchDaemon + UserName과
#   같은 모양이라 두 플랫폼의 운영 방법이 갈리지 않는다. root로 돌지도 않는다.
#
# 갱신을 cron이 아니라 timer로 돌리는 이유:
#   리눅스의 cron은 macOS와 달리 잠자기 문제는 없지만, 머신이 꺼져 있던 동안의
#   잡을 따라잡지 않는 것은 같다. timer의 Persistent=true는 놓친 실행을 다음
#   부팅 때 수행한다 (launchd StartCalendarInterval의 catch-up과 같은 역할).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_UNIT="wterm.service"
RENEW_UNIT="wterm-certrenew.service"
RENEW_TIMER="wterm-certrenew.timer"
UNIT_DIR="/etc/systemd/system"

if [ "$(uname -s)" != "Linux" ]; then
    echo "오류: 이 스크립트는 리눅스용입니다. macOS에서는 scripts/install-launchd.sh를 쓰세요." >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "오류: systemctl이 없습니다 (systemd가 아닌 시스템)." >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "오류: sudo로 실행하세요 — sudo $0 $*" >&2
    exit 1
fi

# sudo 아래에서 실제 사용자를 알아낸다. 이 사용자 권한으로 서비스가 돈다.
RUN_USER="${SUDO_USER:-}"
if [ -z "$RUN_USER" ] || [ "$RUN_USER" = "root" ]; then
    echo "오류: 일반 사용자 계정에서 sudo로 실행해야 합니다 (SUDO_USER 필요)." >&2
    exit 1
fi
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
if [ -z "$RUN_HOME" ] || [ ! -d "$RUN_HOME" ]; then
    echo "오류: $RUN_USER의 홈 디렉터리를 찾을 수 없습니다." >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    for unit in "$RENEW_TIMER" "$RENEW_UNIT" "$SERVER_UNIT"; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/$unit"
        echo "제거됨: $unit"
    done
    systemctl daemon-reload
    echo
    echo "서버는 아직 떠 있을 수 있습니다. 완전히 내리려면: $REPO_DIR/stop.sh"
    exit 0
fi

# ── 로그 디렉터리 ────────────────────────────────────────────────────
# StandardOutput=append: 는 파일은 만들어주지만 디렉터리는 만들지 않는다.
mkdir -p "$REPO_DIR/logs"
chown "$RUN_USER" "$REPO_DIR/logs"

# systemd 240 미만에는 append:가 없다. 그때는 저널로 보낸다 (journalctl -u wterm).
SYSTEMD_VER="$(systemctl --version | head -n1 | awk '{print $2}')"
if [ "${SYSTEMD_VER%%[!0-9]*}" -ge 240 ] 2>/dev/null; then
    SERVER_LOG_OUT="append:$REPO_DIR/logs/wterm.out"
    RENEW_LOG_OUT="append:$REPO_DIR/logs/cert-renew.log"
else
    echo "==> systemd $SYSTEMD_VER: append: 미지원 — 로그를 저널로 보냅니다."
    SERVER_LOG_OUT="journal"
    RENEW_LOG_OUT="journal"
fi

# ── 서버 유닛 ────────────────────────────────────────────────────────
# 시스템 유닛은 로그인 셸을 거치지 않아 PATH가 최소값이다. 서버가 claude/codex를
# 직접 spawn하므로 이들이 있는 경로를 명시해야 한다 (launchd 쪽과 같은 이유).
DAEMON_PATH="$RUN_HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"

cat > "$UNIT_DIR/$SERVER_UNIT" <<EOF
[Unit]
Description=W-Term web terminal
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python -m server
Environment=PATH=$DAEMON_PATH
Environment=HOME=$RUN_HOME
Environment=LANG=ko_KR.UTF-8
# 비정상 종료일 때만 재기동. stop.sh(SIGTERM → 종료 코드 0)로 내린 서버를
# systemd가 곧바로 되살리지 않게 하려는 것 —
# launchd의 KeepAlive{SuccessfulExit:false}와 같은 의도다.
Restart=on-failure
# 중복 기동 거부(종료 코드 3)도 여기서 재시도된다 — 재기동이 늦게 죽는 이전
# 프로세스와 겹쳤을 때 10초 뒤 다시 붙으라는 뜻이고, 그것이 원하는 동작이다.
# 간격이 10초라 systemd 기본 시작 제한(10초에 5회)에도 걸리지 않는다.
RestartSec=10
# 서버는 SIGTERM을 받으면 PTY 세션을 SIGTERM→(최대 10초)→SIGKILL로 회수한 뒤
# 종료한다. 그 시간을 못 기다리고 systemd가 SIGKILL을 보내면 비정상 종료가 되어
# Restart=on-failure에 걸린다. 세션 회수 상한보다 넉넉히 잡는다.
TimeoutStopSec=60
StandardOutput=$SERVER_LOG_OUT
StandardError=$SERVER_LOG_OUT

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT_DIR/$SERVER_UNIT"

# ── 인증서 갱신 유닛 (acme.sh가 있을 때만) ───────────────────────────
# 실행 파일이 PATH에 있을 수도, ~/.acme.sh에만 있을 수도 있다. 홈 경로만 보면
# 갱신 잡을 조용히 건너뛰게 되므로 PATH부터 확인한다 (cert-setup.sh와 같은 방식).
find_acme() {
    local p
    p="$(sudo -u "$RUN_USER" bash -lc 'command -v acme.sh' 2>/dev/null)"
    if [ -n "$p" ] && [ -x "$p" ]; then echo "$p"; return 0; fi
    for p in "$RUN_HOME/.acme.sh/acme.sh" /usr/local/bin/acme.sh \
             /home/linuxbrew/.linuxbrew/bin/acme.sh; do
        if [ -x "$p" ]; then echo "$p"; return 0; fi
    done
    return 1
}
ACME="$(find_acme || true)"
if [ -n "$ACME" ]; then
    echo "==> acme.sh: $ACME"
    # acme.sh가 등록한 crontab 항목을 제거한다. 남겨두면 timer와 이중으로 돈다.
    sudo -u "$RUN_USER" "$ACME" --uninstall-cronjob >/dev/null 2>&1 || true

    cat > "$UNIT_DIR/$RENEW_UNIT" <<EOF
[Unit]
Description=W-Term TLS certificate renewal (acme.sh)

[Service]
Type=oneshot
User=$RUN_USER
ExecStart=$ACME --cron --home $RUN_HOME/.acme.sh
Environment=PATH=$DAEMON_PATH
Environment=HOME=$RUN_HOME
# acme.sh는 date로 인증서 만료일을 파싱하는데, 한글 로케일에서는 영문 월 이름
# ("Nov")을 읽지 못해 갱신 시각 계산의 안전장치가 조용히 꺼진다. C 로케일로 고정한다.
Environment=LC_ALL=C
StandardOutput=$RENEW_LOG_OUT
StandardError=$RENEW_LOG_OUT
EOF
    chmod 644 "$UNIT_DIR/$RENEW_UNIT"

    cat > "$UNIT_DIR/$RENEW_TIMER" <<EOF
[Unit]
Description=W-Term TLS certificate renewal (daily)

[Timer]
OnCalendar=*-*-* 03:27:00
# 머신이 꺼져 있어 놓친 실행을 다음 부팅 때 따라잡는다.
Persistent=true

[Install]
WantedBy=timers.target
EOF
    chmod 644 "$UNIT_DIR/$RENEW_TIMER"
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

systemctl daemon-reload
systemctl enable --now "$SERVER_UNIT"
echo "설치 및 기동됨: $SERVER_UNIT"
if [ "$RENEW_INSTALLED" = 1 ]; then
    systemctl enable --now "$RENEW_TIMER"
    echo "설치 및 기동됨: $RENEW_TIMER"
else
    # 조용히 넘어가면 만료일까지 아무도 모른다. 눈에 띄게 남긴다.
    cat >&2 <<EOF

########################################################################
경고: acme.sh를 찾지 못해 인증서 갱신 타이머를 설치하지 못했습니다.
      이대로 두면 인증서 만료 시 접속이 끊깁니다.
      ./scripts/cert-setup.sh 로 발급을 마친 뒤 이 스크립트를 다시 실행하세요.
      확인: ./scripts/cert-status.sh
########################################################################
EOF
fi

cat <<EOF

==> 완료. 이제 부팅 시 자동으로 기동합니다 (로그인 불필요).

  상태 확인   systemctl status $SERVER_UNIT
  재시작      sudo systemctl restart $SERVER_UNIT   (또는 $REPO_DIR/stop.sh && sudo $REPO_DIR/start.sh)
  중지        $REPO_DIR/stop.sh   (또는 sudo systemctl stop $SERVER_UNIT)
  로그        $REPO_DIR/logs/wterm.out  (또는 journalctl -u $SERVER_UNIT)
  갱신 타이머 systemctl list-timers $RENEW_TIMER
  제거        sudo $0 --uninstall

주의: 코드를 수정한 뒤에는 restart해야 반영됩니다 (reload 미사용).
EOF
