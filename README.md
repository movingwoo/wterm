# W-Term

웹 터미널 시스템. 브라우저(xterm.js) ↔ FastAPI WebSocket ↔ PTY로 실행된 `claude` CLI를 실시간으로 중계한다.

## 특징

- 프로젝트별 라이브 세션 유지, 연결이 끊겨도 grace 기간 동안 프로세스를 보존했다가 재접속 시 화면을 그대로 복원
- `claude --resume` / `--continue` / 새 세션 기동을 모드로 선택 가능
- 같은 프로젝트에 대해 claude 세션과 별도로 셸(`bash -l`) 세션도 동시에 운용 가능
- ssh로 연결 가능한 원격 호스트의 프로젝트도 동일한 UI로 제어 가능
- 선택적 비밀번호 인증, 선택적 유닉스 도메인 소켓 리스닝(리버스 프록시 연동용)
- 프론트엔드 의존성(xterm.js)이 로컬에 내장되어 있어 오프라인에서도 동작, 별도 빌드 과정 없음

## 요구 사항

- Python 3 (시스템 Python에는 pip/venv가 없다는 전제이며, 아래처럼 [uv](https://github.com/astral-sh/uv)로 가상환경을 만든다)
- `claude` CLI (PATH에 설치되어 있어야 함)
- 원격 프로젝트를 쓸 경우: 서버 → 원격 호스트로의 키 기반 ssh 접속, 원격에도 `claude` CLI 설치

## 설치

```bash
~/.local/bin/uv venv .venv
~/.local/bin/uv pip install -p .venv/bin/python -r requirements.txt
```

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
  "password_sha256": "패스워드 hash"
}
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `host`, `port` | O | 바인딩 주소. **반드시 내부 IP(기본 `127.0.0.1`)로 유지**할 것 — 아래 "보안" 참고 |
| `grace_seconds` | O | 연결 해제 후 프로세스를 무손상 유지하는 시간(초). 이후 SIGTERM → 10초 → SIGKILL |
| `projects` | O | 화이트리스트. `path`가 실제 존재하지 않는 로컬 프로젝트는 기동 시 제외됨 |
| `projects[].ssh` | 선택 | `user@host` 형태. 지정하면 claude/셸을 해당 원격 호스트에서 실행 |
| `password_sha256` | 선택 | `echo -n '비밀번호' | sha256sum`. 지정 시에만 로그인 인증 활성화 |
| `uds` | 선택 | 이 경로의 유닉스 도메인 소켓으로 리슨(TCP 대신). 리버스 프록시 연동용 |

`projects.json`은 내부 경로와 비밀번호 해시가 담기므로 `.gitignore`에 포함되어 있다 — 실제 값은 커밋하지 말 것.

## 실행

```bash
./start.sh   # 백그라운드 기동, pid는 logs/wterm.pid
./stop.sh    # 정상 종료 (자식 claude 세션까지 정리)
```

기동 후 브라우저로 `http://<host>:<port>`에 접속한다.

## 구조

```
projects.json         # 화이트리스트 + host/port/grace_seconds 설정 (gitignore됨, 실제 값)
projects.example.json # 위 파일의 예시 템플릿 (커밋 대상)
server/
  config.py            # projects.json 로더
  session.py            # PTY 세션 수명 주기 (spawn/attach/detach/grace/terminate)
  main.py               # FastAPI 앱: GET /, GET /api/projects, WS /ws/{project}
static/
  index.html, app.js, style.css   # Vanilla JS + xterm.js UI
  vendor/                          # xterm.js 로컬 사본 — 직접 수정 금지
```

핵심 동작 방식(WS 프로토콜, 세션 모델, 원격 프로젝트 처리 등)의 상세 설명은 `CLAUDE.md`에 있다.

## 보안

- **임의 명령을 실행할 수 있는 서버다.** 반드시 내부 IP에만 바인딩하고, 앞단 1차 방어수단이 반드시 필요하다. `projects.json`의 `host`를 `0.0.0.0`으로 바꾸지 말 것.
- 비밀번호 인증은 2차 잠금일 뿐이며 전송이 평문 HTTP다.
- 가능하면 TCP 대신 `uds` 옵션으로 리슨해 인바운드 포트 자체를 없애는 편이 더 안전하다.

## 테스트

별도 테스트 프레임워크 없이 WS 스모크 테스트로 검증한다: 새 세션 기동 → ANSI 출력 수신 → input/resize 주입 → 해제 후 재접속 시 버퍼 replay → 서버 종료 시 자식 프로세스 회수. 세션 로직을 고치면 같은 시나리오를 재확인할 것.
