# 자체 HTTPS — 발급, 갱신, 점검

`projects.json`에 `tls_certfile`과 `tls_keyfile`을 **둘 다** 지정하면 앞단에 nginx/Caddy
같은 웹서버 없이 서버가 직접 TLS를 종료한다. 한쪽만 지정하면 HTTPS가 켜지지 않고
경고를 출력한 뒤 HTTP로 기동한다. `uds`와 함께 쓰면 TLS 설정은 무시된다 — 유닉스
소켓은 앞단 프록시가 TLS를 담당하는 구성이기 때문이다.

## 이 서버는 인증서를 발급하지 않는다

파일을 읽기만 하며, ACME 프로토콜은 구현하지도 호출하지도 않는다. 발급과 갱신은
acme.sh 같은 외부 클라이언트의 몫이고, 서버와의 접점은 SIGHUP 하나뿐이다.

```
acme.sh (launchd 스케줄)  ──파일 덮어쓰기 + SIGHUP──▶  W-Term  ──▶  인증서만 교체
```

`SIGHUP`을 받으면 기존 `SSLContext`에 인증서를 다시 물린다. **재시작이 아니라서
라이브 Claude/Codex/셸 세션이 죽지 않는다.** 기존 연결은 그대로 유지되고 새 연결부터
새 인증서를 쓴다. 새 인증서 파일이 깨져 있으면 기존 인증서를 유지한 채 로그만 남기고
서비스는 계속된다.

인증서 출처는 무엇이든 상관없다 — acme.sh, certbot, `tailscale cert`, mkcert, 사설 CA
모두 파일 경로만 맞으면 된다. 아래는 그중 Cloudflare DNS를 쓰는 경우의 전체 순서다.

## Cloudflare DNS로 발급하기

서버가 공개 인터넷에서 도달 불가능해도(예: 테일넷 IP만 가리키는 도메인) DNS-01
방식이라 발급된다.

### 1. DNS A 레코드 등록

접속에 쓸 이름을 서버의 내부 주소로 가리킨다. 예: `wterm.example.com` → `100.x.x.x`.

> ⚠️ **반드시 회색 구름(DNS only)으로 둘 것.** 주황 구름(Proxied)이면 Cloudflare가 그
> 사설/CGNAT 주소로 프록시를 시도하는데 닿을 수 없어 무조건 실패한다.

```bash
dig +short wterm.example.com   # 기대: 100.x.x.x
```

값이 안 나오면 리졸버의 **DNS rebinding 방어**가 사설 대역 응답을 걸러낸 것이다.
접속할 기기마다 확인할 것(공유기, Pi-hole, NextDNS 등에서 발생). 막히면 Tailscale
split DNS로 우회한다.

인증서 발급 자체는 A 레코드 없이도 된다 — DNS-01은 TXT 레코드만 본다. A 레코드는
발급 후 실제 접속을 위한 것이다.

### 2. Cloudflare API 토큰 발급

대시보드 → My Profile → API Tokens에서 **`Zone:DNS:Edit` + `Zone:Zone:Read`** 권한으로
만든다. 계정 전역 API Key는 쓰지 말 것. 토큰 값은 생성 시 한 번만 표시된다.

### 3. 인증서 발급

```bash
export CF_Token=<토큰>
./scripts/cert-setup.sh wterm.example.com
```

토큰에 `Zone:Zone:Read`가 없으면 zone 조회에서 실패한다. 그때는
`export CF_Zone_ID=<zone ID>`를 함께 지정한다.

토큰은 **한 번만 export하면 된다.** acme.sh가 `~/.acme.sh/account.conf`에 저장해두고
갱신 때 재사용한다(평문 저장이므로 백업 시 유의). 스크립트는 인증서를 `~/.wterm/`에
설치하고(`WTERM_CERT_DIR`로 변경 가능), 갱신 후 이 저장소의 `logs/wterm.pid`로 SIGHUP을
보내도록 acme.sh에 등록한다.

### 4. `projects.json`에 반영 후 재시작

출력된 `tls_certfile`/`tls_keyfile` 두 줄을 넣고, 필요하면 `port`도 조정한다.

```bash
./start.sh
```

1024 미만 포트는 macOS/Linux 모두 root 권한이 필요하다. 이 서버는 절대 root로 띄우면
안 되므로(Claude/Codex/셸이 전부 root가 된다) `port`를 `8443` 등으로 두고
`https://도메인:8443`으로 접속한다. 리눅스라면
`sysctl net.ipv4.ip_unprivileged_port_start=443`이나 `setcap CAP_NET_BIND_SERVICE`로
root 없이 443을 열 수 있다 — macOS에는 없는 선택지다.

### 5. 자동 기동 + 갱신 스케줄 설치

```bash
sudo ./scripts/install-launchd.sh    # Linux는 install-systemd.sh
```

**이 단계를 건너뛰면 갱신이 아예 스케줄되지 않는다.** 아래 "자동 갱신" 참고.

### 6. 확인

```bash
./scripts/cert-status.sh   # "갱신 잡  launchd (com.wterm.certrenew) ✅" 가 나와야 정상
```

브라우저에서는 **반드시 도메인으로 접속**한다. IP로 접속하면 인증서 이름이 맞지 않아
경고가 뜬다. 리로드 훅만 따로 확인하려면:

```bash
kill -HUP "$(cat logs/wterm.pid)"   # logs/wterm.log에 "인증서를 다시 읽었습니다"
```

## 자동 갱신은 cron이 아니라 launchd/systemd로

**macOS의 cron은 Mac이 잠들어 있는 동안 실행되지 않고, 깨어나도 놓친 작업을 따라잡지
않는다.** acme.sh가 등록하는 crontab 항목은 하루 한 번 고정된 시각에 도는데, 하필 그
시각에 Mac이 늘 잠들어 있으면 갱신이 영영 돌지 않는다.

`install-launchd.sh`는 acme.sh의 crontab 항목을 제거하고 `com.wterm.certrenew`
LaunchDaemon으로 대체한다. launchd의 `StartCalendarInterval`은 놓친 작업을 깨어날 때
실행하므로 이 문제가 없다. 리눅스에는 잠자기 문제가 없지만 **머신이 꺼져 있던 동안
놓친 잡을 따라잡지 않는 것은 cron도 마찬가지라**, `install-systemd.sh`도 같은 이유로
crontab 항목을 제거하고 `Persistent=true` 타이머(`wterm-certrenew.timer`)로 대체한다.

주의할 점 두 가지 (양쪽 OS 공통):

- **Homebrew로 설치한 acme.sh는 crontab 항목을 자동 등록하지 않는다.** 즉 설치
  스크립트를 돌리기 전까지는 갱신 잡이 cron에도 launchd에도 없는 상태다.
  `cert-status.sh`가 이를 잡아준다.
- `cert-setup.sh`를 나중에 실행했다면 설치 스크립트를 **한 번 더** 돌려야 갱신 데몬이
  설치된다. acme.sh를 못 찾아 건너뛴 경우 스크립트가 마지막에 크게 경고한다.

## acme.sh는 반드시 `LC_ALL=C`로

acme.sh는 `date -j -f "%b %d ..."`로 인증서 만료일을 읽는데, **한글 로케일에서는 `Nov`
같은 영문 월 이름을 파싱하지 못한다.** 그러면 이런 로그가 남는다.

```
Cannot parse _ssldate2time Nov  4 13:00:45 2026 GMT
```

갱신 주기 자체는 발급 시각 기준으로 계산되므로 90일짜리 일반 인증서에서는 실질 피해가
없다. 다만 **"만료일보다 늦게 갱신이 잡히는 것"을 막는 안전장치가 조용히 꺼진다.**
수명이 짧은 인증서를 쓰게 되면 문제가 되므로, 이 저장소의 스크립트는 acme.sh를 항상
`LC_ALL=C`로 호출하고 갱신 데몬 plist에도 `LC_ALL=C`를 넣는다. 손으로 돌릴 때도 붙일 것.

```bash
LC_ALL=C acme.sh --renew -d wterm.example.com --force
```

## 점검

갱신 실패는 **만료일까지 아무 증상이 없다.** 갱신은 됐는데 SIGHUP이 전달되지 않은
경우도 눈에 보이지 않는다. 둘 다 이 명령으로 확인한다.

```bash
./scripts/cert-status.sh
```

만료일과 남은 일수, **비밀 파일 권한**(`projects.json`과 개인키가 `600`인지), **실제
서빙 중인 인증서가 파일과 일치하는지**, 갱신 잡 등록 여부, 마지막 리로드 기록을
출력한다. 이상이 있으면 종료 코드 1. 권한 점검은 TLS를 켜지 않은 구성에서도 돈다.
