#!/usr/bin/env bash
# W-Term TLS 인증서 최초 발급 (Cloudflare DNS-01 + Let's Encrypt).
#
# 이 스크립트는 한 번만 실행하면 된다. acme.sh가 자기 cron 항목을 등록하므로
# 이후 갱신은 자동이고, 갱신될 때마다 --reloadcmd가 W-Term에 SIGHUP을 보내
# 재시작 없이 새 인증서가 반영된다. W-Term 자체는 ACME를 구현하지도, 이
# 스크립트를 호출하지도 않는다 — 인증서 파일을 읽기만 한다.
#
# 사용법:
#   export CF_Token=<Cloudflare API 토큰>
#   ./scripts/cert-setup.sh wterm.example.com
#
# Cloudflare API 토큰은 대시보드 > My Profile > API Tokens에서
# 권한 Zone:DNS:Edit + Zone:Zone:Read 로 만든다. 계정 전역 API Key는 쓰지 말 것.
# 토큰에 Zone:Zone:Read가 없어 zone 조회에 실패하면 CF_Zone_ID도 함께 export한다.
# 토큰은 acme.sh가 ~/.acme.sh/account.conf에 저장하므로 최초 1회만 지정하면 된다.
#
# 인증서 경로는 WTERM_CERT_DIR로 바꿀 수 있다 (기본 ~/.wterm).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CERT_DIR="${WTERM_CERT_DIR:-$HOME/.wterm}"
PID_FILE="$REPO_DIR/logs/wterm.pid"

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    echo "사용법: CF_Token=... $0 <도메인>" >&2
    exit 1
fi

if [ -z "${CF_Token:-}" ]; then
    echo "오류: CF_Token 환경변수가 필요합니다 (Cloudflare API 토큰)." >&2
    echo "      export CF_Token=... 후 다시 실행하세요." >&2
    exit 1
fi

# acme.sh는 PATH에 있거나 기본 설치 경로(~/.acme.sh)에 있다.
if command -v acme.sh >/dev/null 2>&1; then
    ACME="$(command -v acme.sh)"
elif [ -x "$HOME/.acme.sh/acme.sh" ]; then
    ACME="$HOME/.acme.sh/acme.sh"
else
    echo "오류: acme.sh를 찾을 수 없습니다. 아래 중 하나로 설치하세요." >&2
    [ "$(uname -s)" = "Darwin" ] && echo "      brew install acme.sh" >&2 || true
    echo "      curl https://get.acme.sh | sh -s email=<your@email>" >&2
    exit 1
fi

if [ "$(uname -s)" = "Darwin" ]; then
    INSTALLER="install-launchd.sh"
else
    INSTALLER="install-systemd.sh"
fi

mkdir -p "$CERT_DIR"
chmod 700 "$CERT_DIR"

# --server letsencrypt: acme.sh 3.x의 기본 CA는 ZeroSSL이고 이메일 등록을
# 요구한다. 여기서는 등록 절차가 없는 Let's Encrypt를 명시적으로 쓴다.
# LC_ALL=C 필수: acme.sh의 _ssldate2time은 `date -j -f "%b %d ..."`로 인증서
# 만료일을 읽는데, 한글 로케일에서는 "Nov" 같은 영문 월 이름을 파싱하지 못해
# "Cannot parse _ssldate2time"이 뜬다. 그러면 갱신 시각 계산의 만료일 상한과
# "만료 후에 갱신 예정" 경고가 조용히 비활성화된다.
echo "==> 인증서 발급: $DOMAIN (Cloudflare DNS-01)"
LC_ALL=C "$ACME" --issue --server letsencrypt --dns dns_cf -d "$DOMAIN"

# --install-cert가 갱신 시 복사할 위치와 리로드 명령을 acme.sh 설정에 저장한다.
# 서버가 꺼져 있을 때 갱신되면 pid 파일이 없을 수 있으므로 실패해도 넘어간다.
echo "==> 인증서 설치: $CERT_DIR"
LC_ALL=C "$ACME" --install-cert -d "$DOMAIN" \
    --fullchain-file "$CERT_DIR/fullchain.pem" \
    --key-file "$CERT_DIR/key.pem" \
    --reloadcmd "kill -HUP \$(cat '$PID_FILE' 2>/dev/null) 2>/dev/null || true"

chmod 600 "$CERT_DIR/key.pem"

cat <<EOF

==> 완료. projects.json에 아래 두 줄을 추가하고 서버를 재시작하세요.

  "tls_certfile": "$CERT_DIR/fullchain.pem",
  "tls_keyfile":  "$CERT_DIR/key.pem"

접속: https://$DOMAIN:<port>

갱신 잡은 아직 등록되지 않았을 수 있습니다. 반드시 아래를 실행하세요.

  sudo ./scripts/$INSTALLER   # 갱신 잡 등록 + 부팅 시 자동 기동
  ./scripts/cert-status.sh    # "갱신 잡 ... ✅" 가 나와야 정상

갱신 동작을 지금 확인하려면:  $ACME --renew -d $DOMAIN --force
리로드만 확인하려면:          kill -HUP \$(cat "$PID_FILE")
  → logs/wterm.log 에 "인증서를 다시 읽었습니다" 가 남으면 정상입니다.
EOF
