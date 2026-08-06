# W-Term

Claude Code와 Codex를 브라우저에서 원격 제어하는 내부망용 웹 터미널 시스템. 브라우저(xterm.js) ↔ FastAPI WebSocket ↔ PTY로 실행된 `claude`/`codex` CLI를 실시간으로 중계한다.

## 특징

- 프로젝트별 라이브 세션 유지, 연결이 끊겨도 grace 기간 동안 프로세스를 보존했다가 재접속 시 화면을 그대로 복원
- Claude의 `--resume` / `--continue`, Codex의 `resume` / `resume --last`, 새 세션 기동을 지원
- 같은 프로젝트에서 Claude, Codex, 셸(로컬 `$SHELL -l`, 원격은 `bash -l`) 세션을 서로 독립적으로 동시에 운용 가능
- ssh로 연결 가능한 원격 호스트의 프로젝트도 동일한 UI로 제어 가능
- 선택적 비밀번호 인증, 선택적 유닉스 도메인 소켓 리스닝(리버스 프록시 연동용)
- 선택적 자체 HTTPS — 앞단 웹서버 없이 직접 TLS 종료, 인증서 갱신 시 무중단 리로드
- 프론트엔드 의존성(xterm.js)이 로컬에 내장되어 있어 오프라인에서도 동작, 별도 빌드 과정 없음

## 요구 사항

- Python 3 (시스템 Python에는 pip/venv가 없다는 전제이며, 아래처럼 [uv](https://github.com/astral-sh/uv)로 가상환경을 만든다)
- 사용할 AI의 `claude` 및/또는 `codex` CLI (PATH에 설치되어 있어야 함)
- Linux에서 Codex 샌드박스를 사용할 경우: `bubblewrap` 패키지와 비특권 user namespace 지원
- 원격 프로젝트를 쓸 경우: 서버 → 원격 호스트로의 키 기반 ssh 접속, 원격에도 사용할 AI CLI 설치
- 자체 HTTPS를 쓸 경우: [acme.sh](https://github.com/acmesh-official/acme.sh) (`brew install acme.sh` 또는 `curl https://get.acme.sh | sh -s email=<주소>`)

## 설치

```bash
~/.local/bin/uv venv .venv
~/.local/bin/uv pip install -p .venv/bin/python -r requirements.txt
```

Linux에서 Codex 샌드박스를 사용하려면 시스템 `bubblewrap`을 설치한다.

```bash
sudo apt install bubblewrap
```

Ubuntu 24.04에서 user namespace 관련 경고가 계속되면 [Codex sandbox prerequisites](https://developers.openai.com/codex/concepts/sandboxing#prerequisites)의 AppArmor 설정을 적용한다.

## 설정

`projects.example.json`을 `projects.json`으로 복사한 뒤 환경에 맞게 수정한다.

```bash
cp projects.example.json projects.json
```

```json
{
  "host": "127.0.0.1",
  "port": 8877,
  "grace_seconds": 60,
  "projects": [
    { "name": "example-project", "path": "/home/user/example-project" },
    { "name": "remote-project", "path": "/home/user/remote-project", "ssh": "user@remote-host" }
  ],
  "password_hash": "argon2id 해시"
}
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `host`, `port` | O | 바인딩 주소. **반드시 내부 IP(기본 `127.0.0.1`)로 유지**할 것 — 아래 "보안" 참고. 앞단 프록시 없이 자체 HTTPS로 다른 기기에서 접속한다면 루프백이 아니라 그 기기들이 닿을 수 있는 내부 주소(예: 테일넷 IP)여야 한다 |
| `grace_seconds` | O | 연결 해제 후 프로세스를 무손상 유지하는 시간(초). 이후 SIGTERM → 10초 → SIGKILL |
| `projects` | O | 화이트리스트. `path`가 실제 존재하지 않는 로컬 프로젝트는 기동 시 제외됨 |
| `projects[].ssh` | 선택 | `user@host` 형태. 지정하면 Claude/Codex/셸을 해당 원격 호스트에서 실행 |
| `password_hash` | 선택 | argon2id로 생성한 비밀번호 해시. 지정 시에만 로그인 인증 활성화 |
| `allowed_origins` | 선택 | 허용 오리진 목록(스킴/포트 포함). **보통 비워둔다** — 비어 있으면 "Origin의 호스트 == Host 헤더"로 판정하며 이것이 대부분의 구성에서 옳다. 앞단 프록시가 Host를 바꿔 쓰는 경우에만 명시. 아래 "보안" 참고 |
| `uds` | 선택 | 이 경로의 유닉스 도메인 소켓으로 리슨(TCP 대신). 리버스 프록시 연동용 |
| `tls_certfile`, `tls_keyfile` | 선택 | 풀체인/개인키 PEM 경로. **둘 다** 지정하면 HTTPS로 리슨. 아래 "HTTPS" 참고 |

`projects.json`은 내부 경로와 비밀번호 해시가 담기므로 `.gitignore`에 포함되어 있다 — 실제 값은 커밋하지 말 것.

## 실행

```bash
./start.sh   # 기동 (기동 실패 시 로그와 함께 알림)
./stop.sh    # 정상 종료 (자식 Claude/Codex/셸 세션까지 정리)
```

재시작은 `./stop.sh && ./start.sh`. 감시자(launchd/systemd)를 설치했다면 `start.sh`가 **감시자를 통해** 띄우므로 root가 필요하다 — `./stop.sh && sudo ./start.sh`. 직접 띄우면 감시자 밖 프로세스가 되어 크래시해도 되살아나지 않고 환경도 유닛과 달라지기 때문에, 조용히 그쪽으로 빠지지 않고 명령을 안내하고 멈춘다.

`logs/wterm.pid`는 **서버가 직접 쓴다.** 어떤 방식으로 띄우든 같은 파일이 나오므로 `stop.sh`와 인증서 리로드 훅이 동일하게 동작한다. 부팅 시 자동 기동은 아래 "운영"을 참고한다.

이 파일은 동시에 **중복 기동을 막는 잠금**이다. 이미 서버가 떠 있으면 두 번째 프로세스는 (포트가 달라도) 아래처럼 거부되고 종료 코드 3으로 끝난다.

```
wterm.pid: 이미 서버가 실행 중입니다 (pid 1589) — 기동을 중단합니다. 먼저 ./stop.sh 로 내리세요
```

기동 후 브라우저로 `http://<host>:<port>`에 접속한다. TLS를 설정했다면 `https://`.

## HTTPS

`tls_certfile`과 `tls_keyfile`을 지정하면 앞단에 nginx/Caddy 같은 웹서버 없이 서버가 직접 TLS를 종료한다.

**이 서버는 인증서를 발급하지 않는다.** 파일을 읽기만 하며, ACME 프로토콜은 구현하지도 호출하지도 않는다. 발급과 갱신은 acme.sh 같은 외부 클라이언트의 몫이고, 서버와의 접점은 SIGHUP 하나뿐이다.

```
acme.sh (launchd 스케줄)  ──파일 덮어쓰기 + SIGHUP──▶  W-Term  ──▶  인증서만 교체
```

`SIGHUP`을 받으면 기존 `SSLContext`에 인증서를 다시 물린다. **재시작이 아니라서 라이브 Claude/Codex/셸 세션이 죽지 않는다.** 기존 연결은 그대로 유지되고 새 연결부터 새 인증서를 쓴다. 새 인증서 파일이 깨져 있으면 기존 인증서를 유지한 채 로그만 남기고 서비스는 계속된다.

인증서 출처는 무엇이든 상관없다 — acme.sh, certbot, `tailscale cert`, mkcert, 사설 CA 모두 파일 경로만 맞으면 된다.

### Cloudflare DNS로 발급하기 — 전체 순서

도메인이 Cloudflare DNS를 쓴다면 동봉된 스크립트로 설정할 수 있다. 서버가 공개 인터넷에서 도달 불가능해도(예: 테일넷 IP만 가리키는 도메인) DNS-01 방식이라 발급된다.

**1. DNS A 레코드 등록**

접속에 쓸 이름을 서버의 내부 주소로 가리킨다. 예: `wterm.example.com` → `100.x.x.x`(테일넷 IP).

> ⚠️ **반드시 회색 구름(DNS only)으로 둘 것.** 주황 구름(Proxied)이면 Cloudflare가 그 사설/CGNAT 주소로 프록시를 시도하는데 닿을 수 없어 무조건 실패한다.

```bash
dig +short wterm.example.com   # 기대: 100.x.x.x
```

여기서 값이 안 나오면 리졸버의 **DNS rebinding 방어**가 사설 대역 응답을 걸러낸 것이다. 접속할 기기마다 확인할 것(공유기, Pi-hole, NextDNS 등에서 발생). 막히면 Tailscale split DNS로 우회한다.

인증서 발급 자체는 A 레코드 없이도 된다 — DNS-01은 TXT 레코드만 본다. A 레코드는 발급 후 실제 접속을 위한 것이다.

**2. Cloudflare API 토큰 발급**

대시보드 → My Profile → API Tokens에서 **`Zone:DNS:Edit` + `Zone:Zone:Read`** 권한으로 만든다. 계정 전역 API Key는 쓰지 말 것. 토큰 값은 생성 시 한 번만 표시된다.

**3. 인증서 발급**

```bash
export CF_Token=<토큰>
./scripts/cert-setup.sh wterm.example.com
```

토큰에 `Zone:Zone:Read`가 없으면 zone 조회에서 실패한다. 그때는 `export CF_Zone_ID=<zone ID>`를 함께 지정한다.

토큰은 **한 번만 export하면 된다.** acme.sh가 `~/.acme.sh/account.conf`에 저장해두고 갱신 때 재사용한다(평문 저장이므로 백업 시 유의). 스크립트는 인증서를 `~/.wterm/`에 설치하고(`WTERM_CERT_DIR`로 변경 가능), 갱신 후 이 저장소의 `logs/wterm.pid`로 SIGHUP을 보내도록 acme.sh에 등록한다.

**4. `projects.json`에 반영 후 재시작**

출력된 `tls_certfile`/`tls_keyfile` 두 줄을 넣고, 필요하면 `port`도 조정한다(아래 "주의" 참고).

```bash
./start.sh
```

**5. 자동 기동 + 갱신 스케줄 설치**

```bash
sudo ./scripts/install-launchd.sh
```

**이 단계를 건너뛰면 갱신이 아예 스케줄되지 않는다.** acme.sh를 Homebrew로 설치한 경우 crontab 항목이 자동 등록되지 않으므로, 이 스크립트를 돌리기 전까지는 만료 시 접속이 끊긴다.

**6. 확인**

```bash
./scripts/cert-status.sh   # "갱신 잡  launchd (com.wterm.certrenew) ✅" 가 나와야 정상
```

브라우저에서는 **반드시 도메인으로 접속**한다. IP로 접속하면 인증서 이름이 맞지 않아 경고가 뜬다.

리로드 훅만 따로 확인하려면:

```bash
kill -HUP "$(cat logs/wterm.pid)"   # logs/wterm.log에 "인증서를 다시 읽었습니다"
```

### 주의

- macOS/Linux 모두 1024 미만 포트는 root 권한이 필요하다. 이 서버는 절대 root로 띄우면 안 되므로(Claude/Codex/셸이 전부 root가 된다) `port`를 `8443` 등으로 두고 `https://도메인:8443`으로 접속한다. 443을 쓰려면 커널 레벨 포트 리다이렉트나 launchd 소켓 액티베이션이 필요하다.
- `uds`와 함께 쓰면 TLS 설정은 무시된다. 유닉스 소켓은 앞단 프록시가 TLS를 담당하는 구성이기 때문이다.
- 한쪽만 지정하면 HTTPS가 켜지지 않고 경고를 출력한 뒤 HTTP로 기동한다.

## 운영

### 부팅 시 자동 기동 (macOS)

```bash
sudo ./scripts/install-launchd.sh              # 설치
sudo ./scripts/install-launchd.sh --uninstall  # 제거
```

LaunchAgent가 아니라 **LaunchDaemon + `UserName`**을 설치한다. LaunchAgent는 "로그인 시"에만 뜨기 때문에, 재부팅 후 물리적으로 로그인해야 서버가 올라온다면 원격 접속 용도로 쓸모가 없다. LaunchDaemon은 부팅 시 뜨고, `UserName` 지정으로 root가 아닌 해당 사용자 권한으로 실행된다.

로그인 셸을 거치지 않아 `PATH`가 최소값이 되므로, 설치 스크립트가 `~/.local/bin`과 `/opt/homebrew/bin`을 명시적으로 넣어준다(서버가 `claude`/`codex`를 직접 spawn하기 때문). 재부팅 후에는 실제로 세션이 기동되는지 한 번 확인할 것.

```bash
./stop.sh && sudo ./start.sh                          # 재시작 (코드 수정 후 필수)
sudo launchctl bootout    system/com.wterm.server     # 등록 해제 (부팅 시에도 안 뜸)
sudo launchctl print       system/com.wterm.server    # 상태
```

재시작에 `launchctl kickstart -k`를 쓰지 말 것. `-k`는 실행 중인 인스턴스를 죽이는 것이라 `SIGTERM` 경로를 타지 않고, PTY 세션 회수가 건너뛰어진다 — 세션들은 자기 프로세스 그룹에 있어서 **서버만 사라지고 살아남는다.** `stop.sh`는 root 없이도 동작한다(정상 종료라 launchd가 되살리지 않는다).

`KeepAlive`는 비정상 종료일 때만 재기동하도록 설정되어 있어, `./stop.sh`로 내린 서버를 launchd가 곧바로 되살리지 않는다. 단 `stop.sh`가 20초 폴백으로 SIGKILL까지 갔다면 비정상 종료로 보여 되살아난다 — 이때는 스크립트가 완전히 내리는 명령을 함께 출력한다.

### 부팅 시 자동 기동 (Linux)

```bash
sudo ./scripts/install-systemd.sh              # 설치
sudo ./scripts/install-systemd.sh --uninstall  # 제거
```

launchd 쪽과 같은 모양이다: **시스템 유닛 + `User=`**. 사용자 유닛(`systemd --user`)은 그 사용자의 로그인 세션이 있어야 뜨고, linger를 켜서 우회하더라도 설정이 계정 상태에 숨어 있어 "왜 안 떴는지"를 추적하기 어렵다.

```bash
./stop.sh && sudo ./start.sh             # 재시작 (코드 수정 후 필수)
sudo systemctl restart wterm             # 같음 — systemd의 stop은 SIGTERM이라 정상 종료다
sudo systemctl stop    wterm             # 중지
systemctl status       wterm             # 상태
systemctl list-timers  wterm-certrenew.timer
```

`Restart=on-failure`가 launchd의 `KeepAlive{SuccessfulExit:false}`와 같은 역할을 한다. 인증서 갱신은 `wterm-certrenew.timer`(`OnCalendar` + `Persistent=true`)가 담당하며, `Persistent=true`가 머신이 꺼져 있던 동안 놓친 실행을 다음 부팅 때 따라잡는다.

리눅스에서는 `sysctl net.ipv4.ip_unprivileged_port_start=443`이나 `setcap CAP_NET_BIND_SERVICE`로 root 없이 443을 열 수 있다 — macOS에 없는 선택지다.

> Linux 경로는 작성돼 있지만 아직 실제 리눅스 머신에서 검증하지 않았다.

### 인증서 점검

갱신 실패는 **만료일까지 아무 증상이 없다.** 갱신은 됐는데 SIGHUP이 전달되지 않은 경우도 눈에 보이지 않는다. 둘 다 이 명령으로 확인한다.

```bash
./scripts/cert-status.sh
```

만료일과 남은 일수, 개인키 권한, **실제 서빙 중인 인증서가 파일과 일치하는지**, 갱신 잡 등록 여부, 마지막 리로드 기록을 출력한다. 이상이 있으면 종료 코드 1.

### 갱신 잡은 cron이 아니라 launchd로

**macOS의 cron은 Mac이 잠들어 있는 동안 실행되지 않고, 깨어나도 놓친 작업을 따라잡지 않는다.** acme.sh가 등록하는 crontab 항목은 하루 한 번 고정된 시각에 도는데, 하필 그 시각에 Mac이 늘 잠들어 있으면 갱신이 영영 돌지 않는다.

`install-launchd.sh`는 acme.sh의 crontab 항목을 제거하고 `com.wterm.certrenew` LaunchDaemon으로 대체한다. launchd의 `StartCalendarInterval`은 놓친 작업을 깨어날 때 실행하므로 이 문제가 없다.

주의할 점 두 가지:

- **Homebrew로 설치한 acme.sh는 crontab 항목을 자동 등록하지 않는다.** 즉 `install-launchd.sh`를 돌리기 전까지는 갱신 잡이 cron에도 launchd에도 없는 상태다. `cert-status.sh`가 이를 잡아준다.
- `cert-setup.sh`를 나중에 실행했다면 `install-launchd.sh`를 **한 번 더** 돌려야 갱신 데몬이 설치된다. acme.sh를 못 찾아 갱신 데몬을 건너뛴 경우 스크립트가 마지막에 크게 경고한다.

리눅스에는 잠자기 문제가 없지만, **머신이 꺼져 있던 동안 놓친 잡을 따라잡지 않는 것은 cron도 마찬가지다.** `install-systemd.sh`도 같은 이유로 acme.sh의 crontab 항목을 제거하고 `Persistent=true` 타이머로 대체한다. 위 주의사항 두 가지는 리눅스에서도 그대로 적용된다.

### acme.sh는 반드시 `LC_ALL=C`로

acme.sh는 `date -j -f "%b %d ..."`로 인증서 만료일을 읽는데, **한글 로케일에서는 `Nov` 같은 영문 월 이름을 파싱하지 못한다.** 그러면 이런 로그가 남는다.

```
Cannot parse _ssldate2time Nov  4 13:00:45 2026 GMT
```

갱신 주기 자체는 발급 시각 기준으로 계산되므로 90일짜리 일반 인증서에서는 실질 피해가 없다. 다만 **"만료일보다 늦게 갱신이 잡히는 것"을 막는 안전장치가 조용히 꺼진다.** 수명이 짧은 인증서를 쓰게 되면 문제가 되므로, 이 저장소의 스크립트는 acme.sh를 항상 `LC_ALL=C`로 호출하고 갱신 데몬 plist에도 `LC_ALL=C`를 넣는다. 손으로 acme.sh를 돌릴 때도 붙일 것.

```bash
LC_ALL=C acme.sh --renew -d wterm.example.com --force
```

## 구조

```
projects.json         # 화이트리스트 + host/port/grace_seconds/TLS 설정 (gitignore됨, 실제 값)
projects.example.json # 위 파일의 예시 템플릿 (커밋 대상)
start.sh / stop.sh    # 백그라운드 기동 / 정상 종료. pid는 서버가 logs/wterm.pid에 기록
server/
  config.py            # projects.json 로더
  session.py            # Claude/Codex/셸 PTY 세션 수명 주기
  main.py               # FastAPI 앱: GET /, GET /api/projects, WS /ws/{project}
static/
  index.html, app.js, style.css   # Vanilla JS + xterm.js UI
  vendor/                          # xterm.js 로컬 사본 — 직접 수정 금지
tests/                  # pytest 스모크 스위트 (아래 "테스트")
scripts/
  cert-setup.sh          # TLS 인증서 최초 발급(acme.sh + Cloudflare DNS-01). 1회 실행
  cert-status.sh         # 인증서 만료/서빙 일치/갱신 잡 점검
  install-launchd.sh     # (macOS) 부팅 자동 기동 + 갱신 잡 launchd 전환. sudo 필요
  install-systemd.sh     # (Linux) 위와 같은 역할의 systemd 유닛/타이머. sudo 필요
```

핵심 동작 방식, 세션 불변조건, WS 프로토콜, 개발 및 검증 규칙은 `AGENTS.md`에 있다. Claude Code로 이 저장소를 작업할 때의 추가 지침은 `CLAUDE.md`를 참고한다.

## 보안

- **임의 명령을 실행할 수 있는 서버다.** 반드시 내부 IP에만 바인딩하고, 앞단 1차 방어수단이 반드시 필요하다. `projects.json`의 `host`를 `0.0.0.0`으로 바꾸지 말 것.
- **TLS는 1차 방어수단이 아니다.** HTTPS를 켜도 전송 구간이 암호화될 뿐 접근 통제는 그대로다. VPN/테일넷 같은 네트워크 경계는 여전히 필요하며, TLS를 켰다고 공개 인터넷에 노출해도 되는 것이 아니다.
- 비밀번호 인증은 2차 잠금일 뿐이다. 평문 HTTP로 운용하면 비밀번호와 터미널 내용이 그대로 노출되므로, 로컬 루프백을 벗어나는 구성이라면 TLS를 켜거나 앞단 프록시를 둘 것.
- **Origin 검증.** WebSocket 핸드셰이크에는 CORS가 적용되지 않아, 쿠키만 확인하면 로그인해 둔 사용자가 방문한 아무 웹페이지나 이 서버로 소켓을 열 수 있다(CSWSH → 임의 명령 실행). 그래서 WS와 POST는 Origin을 검사하며, 기본 규칙은 `allowed_origins`가 비어 있을 때의 "Origin 호스트 == Host 헤더"다. Origin이 없는 요청(브라우저가 아닌 클라이언트)도 거부한다.
  접속이 안 되는데 원인이 안 보이면 `logs/wterm.log`에서 `오리진 불일치로 WS 거절`을 확인할 것 — 오리진 거절은 accept 전에 일어나 브라우저에는 평범한 연결 실패로만 보인다.
- **로그인 시도 제한.** argon2 검증 1회가 64 MiB / 약 35ms를 쓰므로, 제한이 없으면 인증 없이 누구나 그 비용을 태워 서버를 마비시킬 수 있다. 버킷별 지수 백오프(5회 유예 → 최대 15분)와 전역 동시 검증 수 제한을 함께 건다. `uds`나 프록시 뒤에서는 클라이언트 IP를 알 수 없어 전부 한 버킷으로 모이므로, 그 구성에서는 **앞단이 IP별 제한을 맡아야 한다.**
- **세션 토큰 만료는 서버가 판정한다.** 쿠키 `max-age`는 브라우저에게 하는 부탁일 뿐이라 그것만으로는 탈취된 토큰이 영원히 유효하다. 토큰은 발급 30일 후 서버에서 거부되고, 사이드바의 "로그아웃"으로 즉시 폐기할 수 있다. 재시작은 폐기 수단으로 쓸 수 없다 — PTY 세션이 전부 죽는다.
- 인증서 개인키는 `600`으로 두고 저장소 밖에 보관한다(`cert-setup.sh` 기본값 `~/.wterm/`).
- 가능하면 TCP 대신 `uds` 옵션으로 리슨해 인바운드 포트 자체를 없애는 편이 더 안전하다.

## 테스트

```bash
.venv/bin/python -m pip install -r requirements-dev.txt   # 최초 1회
.venv/bin/python -m pytest
```

pytest 스모크 스위트(`tests/`)가 있다. 테스트는 저장소 **사본**에서 서버를 실제 프로세스로 띄우므로 운영 `projects.json`이나 실행 중인 서버를 건드리지 않는다. 새 세션 기동 → 입력/리사이즈 → 재접속 replay → 종료 시 자식 회수까지 진짜 셸을 왕복시키고, 인증(Origin/로그인 제한/토큰 폐기), pid 파일 단일 인스턴스 잠금, SIGTERM 종료 코드, SIGHUP 인증서 리로드도 함께 본다.

GitHub Actions(`.github/workflows/ci.yml`)가 main 푸시와 PR마다 같은 스위트를 ubuntu(3.10/3.13)와 macOS(3.10, 운영 환경과 같은 버전)에서 돌린다. 의존성이 하한만 지정돼 있어(`requirements.txt`) 최신 조합이 깨지는 것도 여기서 먼저 드러난다.

리눅스 러너에서는 `systemd` 잡이 `scripts/install-systemd.sh`를 실제로 설치해 유닛 기동 → SIGKILL 시 재기동 → `stop.sh` 후 되살아나지 않음 → `cert-status.sh` → 제거까지 왕복시킨다. 재부팅 자동 기동과 systemd 240 미만의 `append:` 폴백은 러너에서 재현할 수 없어 여전히 실기 확인 대상이다.

`claude`/`codex` CLI는 필요 없다 — 실기 세션은 로그인 셸로 확인하고, 에이전트 경로는 "바이너리가 PATH에 없을 때 exit 127을 알린다"까지만 본다. Claude/Codex 실제 동작은 브라우저에서 확인할 것.
