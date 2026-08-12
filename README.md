# W-Term

브라우저에서 Claude Code와 Codex를 원격 제어하는 내부망용 웹 터미널.

브라우저(xterm.js) ↔ FastAPI WebSocket ↔ PTY로 실행된 `claude`/`codex` CLI를 실시간으로 중계합니다.  
노트북을 덮고 나가도 서버의 세션은 계속 돌고, 폰에서 다시 열면 화면이 그대로 이어집니다.

> ⚠️ **임의 명령을 실행할 수 있는 서버입니다.**  
> 반드시 내부 IP에만 바인딩하고 VPN/테일넷 같은 네트워크 경계 뒤에서만 운용해야 합니다.  
> 자세한 내용은 [docs/security.md](docs/security.md).

## 특징

- **세션이 끊겨도 살아남음.** 연결이 끊기면 grace 기간 동안 프로세스를 보존했다가 재접속하면 화면을 그대로 복원합니다.
- **한 프로젝트에서 Claude, Codex, 셸을 동시에.** 세 세션이 서로 독립적으로 유지됩니다.
- **탭과 좌우 분할.** 여러 세션을 탭으로 열어두고, 큰 화면에서는 창을 좌우로 나눠 둘을 동시에 봅니다. 백그라운드 탭에 출력이 생기면 탭에 표시되고, 새로고침하면 보던 탭이 그대로 돌아옵니다(주소로 공유도 됩니다).
- **터미널 버퍼 검색.** 검색 버튼이나 Ctrl+F(macOS는 ⌘F)로 현재 탭의 스크롤백을 찾고 이전·다음 결과로 이동합니다. 분할된 두 창은 각자 검색합니다.
- **폰에서도 제대로.** 프로젝트 목록은 터미널 위에 서랍으로 열리고 세션을 고르면 자동으로 닫힙니다. 소프트 키보드에 없는 Esc·Tab·Ctrl·방향키를 키 바로 보내고, 터미널 글자는 12~20px에서 조절합니다.
- **기다릴 때 알려줌.** Claude의 터미널 벨과 Codex의 OSC 9 알림을 받아 탭과 문서 제목에 표시하고, 허용했다면 브라우저 알림도 띄웁니다.
- **시스템·라이트·다크 테마.** 기본은 운영체제 설정을 따르고, 열린 터미널의 ANSI 색까지 함께 바뀝니다.
- **데스크톱 복사·붙여넣기.** macOS는 ⌘C/⌘V, Windows·Linux는 Ctrl+C/Ctrl+V를 씁니다. 선택이 없는 Ctrl+C는 그대로 실행 중인 명령을 중단합니다.
- **이어하기 지원.** Claude의 `--resume`/`--continue`, Codex의 `resume`/`resume --last`.
- **원격 프로젝트.** ssh로 닿는 호스트의 프로젝트도 같은 UI로 제어합니다.
- **프로젝트별 실행 설정.** 프로젝트마다 Claude/Codex CLI 인자와 공통 환경변수를 지정하며 로컬·SSH 세션에 같은 방식으로 적용합니다.
- **즉시 갱신되는 세션 상태.** 프로젝트 목록은 별도 상태 WebSocket으로 받아 세션 시작·종료를 폴링 지연 없이 반영합니다.
- **자체 HTTPS.** 앞단 웹서버 없이 직접 TLS를 종료하고, 인증서 갱신 시 세션을 죽이지 않고 무중단 리로드합니다.
- **빌드 과정 없음.** xterm.js가 저장소에 내장돼 있어 오프라인에서도 그대로 뜹니다.

## 요구 사항

- Python 3 (시스템 Python에 pip/venv가 없다는 전제로 아래에서 [uv](https://github.com/astral-sh/uv)를 사용)
- 쓰려는 AI의 `claude` 및/또는 `codex` CLI가 PATH에 설치돼 있을 것
- (원격 프로젝트) 서버 → 원격 호스트로의 키 기반 ssh 접속, 원격에도 AI CLI 설치
- (Linux + Codex 샌드박스) `bubblewrap` 패키지와 비특권 user namespace 지원

## 설치

```bash
~/.local/bin/uv venv .venv
~/.local/bin/uv pip install -p .venv/bin/python -r requirements.txt
```

Linux에서 Codex 샌드박스를 쓴다면 `sudo apt install bubblewrap`도 필요합니다.  
Ubuntu 24.04에서 user namespace 경고가 계속되면 [Codex sandbox prerequisites](https://learn.chatgpt.com/docs/sandboxing)의 AppArmor 설정을 적용합니다.

## 설정

```bash
cp projects.example.json projects.json
```

```json
{
  "host": "127.0.0.1",
  "port": 8877,
  "grace_seconds": 60,
  "projects": [
    {
      "name": "example-project",
      "path": "/home/user/example-project",
      "args": {
        "claude": ["--model", "your-claude-model"],
        "codex": ["--model", "your-codex-model"]
      },
      "env": { "WTERM_PROJECT_KIND": "example" }
    },
    { "name": "remote-project", "path": "/home/user/remote-project", "ssh": "user@remote-host" }
  ],
  "password_hash": null
}
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `host`, `port` | O | 바인딩 주소와 1~65535 포트. 기본은 `127.0.0.1:8877`. loopback 밖의 TCP는 아래 안전 조건을 만족하지 않으면 기동 거부 |
| `grace_seconds` | O | 0~86400초. 연결 해제 후 프로세스를 무손상 유지하고, 이후 SIGHUP → SIGTERM → SIGKILL 순으로 프로세스 그룹을 정리 |
| `idle_seconds` | 선택 | 0~31536000초. 양쪽 방향 모두 이 시간 동안 조용한 세션을 종료. 기본 `0`(끔). 출력이 계속 나오는 자동 실행은 종료되지 않음 |
| `projects` | O | 화이트리스트. `path`가 없는 로컬 프로젝트는 기동 시 제외됨 |
| `projects[].ssh` | 선택 | `user@host`. 지정하면 Claude/Codex/셸을 해당 원격 호스트에서 실행 |
| `projects[].args` | 선택 | `claude`/`codex`별 CLI 전역 인자의 문자열 배열. 셸 문자열이 아니며 resume/continue 인자보다 앞에 적용. 옵션 종결자 `--`는 지정 불가 |
| `projects[].env` | 선택 | 해당 프로젝트의 Claude/Codex/셸에 공통 적용할 환경변수 문자열 맵. `TERM`은 W-Term이 관리하므로 지정 불가 |
| `password_hash` | 선택 | 유효한 Argon2id 해시. 지정 시에만 로그인 인증이 켜짐 |
| `allowed_origins` | 선택 | path/query/fragment 없는 완전한 HTTP(S) origin 목록. **보통 비워둠** — 비어 있으면 "Origin 호스트 == Host 헤더"로 판정. 앞단 프록시가 Host를 바꿔 쓸 때만 명시 |
| `uds` | 선택 | 이 경로의 유닉스 도메인 소켓으로 리슨(TCP 대신). 리버스 프록시 연동용 |
| `tls_certfile`, `tls_keyfile` | 선택 | 풀체인/개인키 PEM 경로. **항상 둘을 함께** 지정하며, 한쪽만 있으면 기동 실패 |
| `allow_insecure_tcp` | 선택 | 기본 `false`. loopback 밖의 무인증 또는 평문 TCP를 감수한다는 명시적 위험 승인. 정상 배포에서는 사용하지 말 것 |

패스워드 해시는 이렇게 만듭니다.

```bash
.venv/bin/python -c "from argon2 import PasswordHasher; from getpass import getpass; print(PasswordHasher().hash(getpass('패스워드: ')))"
```

출력된 전체 문자열로 예시의 `password_hash: null`을 교체하면 인증이 켜집니다.

또는 W-Tools의 비밀번호 해시 생성 기능을 이용합니다.  
[https://wtools.movingwoo.com/#/tool/password-hash](https://wtools.movingwoo.com/#/tool/password-hash)

`projects[].args`와 `projects[].env`는 새로 시작하는 세션부터 적용됩니다.  
실행 중인 세션은 설정을 다시 읽어 명령이나 환경을 바꾸지 않습니다.  
두 필드는 프로젝트 목록 API와 감사 로그에 기록되지 않지만 `projects.json` 자체에는 평문으로 있으므로 기존과 같이 소유자만 읽을 수 있는 권한(`600`)을 유지하길 권장합니다.

TCP로 직접 다른 기기에서 접속할 때는 loopback 대신 사설 VPN 인터페이스 주소를 쓰고 `password_hash`와 자체 TLS를 모두 설정해야 합니다.  
UDS 뒤에서 TLS를 종료하는 프록시는 이 검사에서 제외됩니다.  
`0.0.0.0` 평문처럼 보호 경계가 없는 구성을 정말 써야 할 때만 `allow_insecure_tcp: true`를 명시할 수 있으며 이 값은 암호화나 인증을 제공하지 않습니다.

원격 프로젝트의 `이어하기`/자동 재접속은 비대화식 SSH로 기록 존재를 먼저 확인합니다.  
연결, 인증, 호스트 키 또는 원격 명령 확인이 실패하면 새 세션으로 조용히 폴백하지 않고 터미널에 원인을 알린 뒤 멈춥니다.  
연결 상태를 고친 뒤 같은 버튼으로 재시도하세요.

`projects.json`은 내부 경로와 패스워드 해시가 담기므로 실제 값은 커밋하지 않도록 주의합니다.

## 실행

```bash
./start.sh   # 기동
./stop.sh    # 정상 종료 (자식 Claude/Codex/셸 세션까지 정리)
```

브라우저로 `http://<host>:<port>`에 접속하면 됩니다.  
재시작은 `./stop.sh && ./start.sh`

사이드바에서 세션을 열면 터미널 위에 탭으로 쌓입니다.  
이미 열려 있는 세션의 버튼은 "보기"로 바뀌어 연결을 건드리지 않고 그 탭으로만 이동합니다.  
탭 전환은 클릭 또는 `Ctrl+Alt+←/→`, 닫기는 `×`나 가운데 클릭입니다.  
**탭을 닫아도 세션은 죽지 않습니다**.  
소켓만 떨어지므로 서버는 `grace_seconds` 동안 프로세스를 붙들고 있고 그 안에 다시 열면 복원됩니다.  
세션을 실제로 끝내려면 사이드바의 "종료" 버튼을 씁니다.

넓은 화면(900px 이상)에서는 탭 줄 오른쪽의 "분할" 버튼으로 창을 좌우 둘로 나눌 수 있습니다.  
새로 연 세션은 그때 포커스된 창에 뜨고, 이미 열린 탭은 `⇥`로 옆 창에 보냅니다.  
가운데 경계를 끌어 너비를 조절하며, "합치기"를 누르면 그 창의 탭이 옆 창으로 합쳐집니다.  
창을 좁히면 자동으로 하나로 합쳐집니다 — 터미널은 좁아지면 읽기 불편한 정도가 아니라 TUI가 깨지기 때문에, 나눌 수 없는 폭에서는 분할 자체를 막습니다.

폰과 터치 중심 장치에서는 사이드바가 터미널을 좁히지 않고 화면 위에 서랍으로 열립니다.  
세션을 고르면 자동으로 닫히며, 화면 왼쪽 핸들로 다시 열 수 있습니다.  
모바일 터미널의 기본 글자는 16px이고 키 바의 `A−`·`A+`로 12~20px 사이에서 바꿉니다.  
선택한 크기는 새로고침 뒤에도 유지되고 열린 모든 탭에 함께 적용됩니다.

여기까지가 최소 구성이며 실제로 다른 기기에서 쓰려면 아래 두 가지를 더 설정합니다.

- **HTTPS 켜기** → [docs/https.md](docs/https.md). 인증서 발급(Cloudflare DNS-01), 무중단 갱신, 점검까지
- **부팅 시 자동 기동** → [docs/operations.md](docs/operations.md)  
  macOS는 launchd, Linux는 systemd. `sudo ./scripts/install-launchd.sh` 한 줄이지만 재시작 방법이 달라지므로 한 번은 읽을 것

## 보안

앞 단에 네트워크 경계가 있다는 전제 하에 서버는 아래와 같은 보안 정책을 가집니다.

- Origin 검증 (WebSocket에는 CORS가 없어 CSWSH로 임의 명령이 실행될 수 있음)
- argon2id 패스워드 인증 + 로그인 시도 제한 (지수 백오프, 동시 검증 수 제한)
- 서버가 판정하는 30일 토큰 만료와 즉시 로그아웃
- CSP를 포함한 응답 헤더, 비밀 파일 권한(`600`) 점검
- 90일 보관 감사 로그 — **터미널 내용은 남기지 않음**

각 항목의 이유와 **침해가 의심될 때의 자격증명 회전 순서**는 [docs/security.md](docs/security.md)에 있습니다.

## 구조

```
projects.json         # 화이트리스트 + host/port/grace/TLS 설정 (gitignore됨)
projects.example.json # 위 파일의 예시 템플릿
THIRD_PARTY_NOTICES.md # vendored xterm.js와 addon의 버전·라이선스 고지
start.sh / stop.sh    # 기동 / 정상 종료. pid는 서버가 logs/wterm.pid에 기록
server/
  config.py           # projects.json 로더
  session.py          # Claude/Codex/셸 PTY 세션 수명 주기
  auth.py             # 토큰·로그인 제한·열린 WebSocket 인증 폐기
  audit.py            # 감사 로그 정규화·throttling·90일 보관 handler
  project_status.py   # 프로젝트 스냅샷과 상태 WebSocket broadcast
  main.py             # FastAPI 라우트와 Origin/터미널 프로토콜, TLS/PID 조립
static/
  index.html, app.js, style.css   # Vanilla JS + xterm.js UI
  modules/                        # 빌드 없는 프로젝트/테마/키 바 ES 모듈
  vendor/                         # xterm.js 로컬 사본 — 직접 수정 금지
tests/                # pytest 스모크 스위트
scripts/
  cert-setup.sh       # TLS 인증서 최초 발급(acme.sh + Cloudflare DNS-01). 1회 실행
  cert-status.sh      # 인증서 만료/서빙 일치/갱신 잡 + 비밀 파일 권한 점검
  install-launchd.sh  # (macOS) 부팅 자동 기동 + 갱신 잡 launchd 전환. sudo 필요
  install-systemd.sh  # (Linux) 위와 같은 역할의 systemd 유닛/타이머. sudo 필요
```

## 테스트

```bash
.venv/bin/python -m pip install -r requirements-dev.txt   # 최초 1회
.venv/bin/python -m pytest
```

저장소 **사본**에서 서버를 실제 프로세스로 띄우므로 운영 `projects.json`이나 실행 중인 서버를 건드리지 않습니다.  
진짜 셸로 세션 기동 → 입력/리사이즈 → 재접속 replay → 자식회수까지 왕복하고 인증·pid 잠금·SIGTERM 종료 코드·SIGHUP 인증서 리로드도 함께 봅니다.  
`claude`/`codex` CLI는 필요 없습니다.

GitHub Actions가 main 푸시와 PR마다 macOS와 Ubuntu 양쪽에서 같은 스위트를 돌리고 리눅스에서는 systemd 유닛 설치까지 실제로 왕복시킵니다.

UI를 바꾼 릴리즈는 pytest에 더해 [브라우저 체크리스트](docs/release-checklist.md)를 실제 화면에서 확인합니다.

## 문서

| 파일 | 내용 |
| --- | --- |
| [docs/https.md](docs/https.md) | 인증서 발급·무중단 갱신·점검 |
| [docs/operations.md](docs/operations.md) | 기동/재시작, 부팅 시 자동 기동 |
| [docs/security.md](docs/security.md) | 위협 전제, 방어 구현, 침해 시 회전 순서 |
| [docs/release-checklist.md](docs/release-checklist.md) | UI 변경 릴리즈의 데스크톱·모바일 브라우저 점검 |
| [AGENTS.md](AGENTS.md) | 세션 불변조건, WS 프로토콜, 개발·검증 규칙 |
| [CHANGELOG.md](CHANGELOG.md) | 릴리즈 색인 (상세 내역은 GitHub Releases) |
| [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) | vendored xterm.js와 addon의 버전·라이선스 |
