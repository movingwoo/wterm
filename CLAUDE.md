# W-Term

VPN 내부망에서 사용하는 Claude Code 웹 원격 제어/모니터링 시스템. 브라우저(xterm.js) ↔ FastAPI WebSocket ↔ PTY로 실행된 `claude` CLI를 실시간으로 중계한다.

## 실행

```bash
./start.sh           # projects.json의 host/port로 기동 (예: 127.0.0.1:8877)
```

- 시스템 Python에는 pip/venv가 없다. 의존성은 `~/.local/bin/uv`로 만든 `.venv/`에 설치되어 있다.
  재설치: `uv venv .venv && uv pip install -p .venv/bin/python -r requirements.txt`
- 프론트엔드 의존성(xterm.js 5.5.0, addon-fit 0.10.0)은 `static/vendor/`에 로컬로 내장되어 있어 오프라인에서도 동작한다. 빌드 과정 없음.

## 구조

```
projects.json        # 화이트리스트 + host/port/grace_seconds 설정 (프로젝트 추가는 여기에)
server/
  config.py          # projects.json 로더. 존재하지 않는 디렉터리는 기동 시 제외
  session.py         # PTY 세션 수명 주기 (spawn/attach/detach/grace/terminate)
  main.py            # FastAPI 앱: GET /, GET /api/projects, WS /ws/{project}
static/
  index.html, app.js, style.css   # Vanilla JS + xterm.js UI (좌측 프로젝트 목록 / 우측 터미널)
  static/vendor/     # xterm.js 로컬 사본 — 직접 수정 금지
```

## 핵심 동작 방식

- **세션 모델:** 프로젝트(cwd)당 라이브 세션 최대 1개 (`SessionManager.sessions`). DB·로그 없음(무상태). 세션 기록은 Claude CLI 자체의 `~/.claude/projects/` 저장에 의존한다.
- **PTY:** `pty.fork()` 사용 — setsid + 제어 터미널 설정이 자동으로 되어 Ctrl-C 등 시그널 전달이 정상 동작한다. master fd는 non-blocking으로 만들고 `loop.add_reader()`로 읽는다. `os.read()`가 `OSError(EIO)`를 던지면 자식 종료 신호다.
- **WS 프로토콜:** 클라이언트→서버는 JSON 텍스트(`{"type":"input","data":..}`, `{"type":"resize","cols","rows"}`), 서버→클라이언트는 바이너리(raw 터미널 출력) + JSON 텍스트(`status`/`exit`). 새 메시지 타입을 추가하면 `server/main.py`의 수신 루프와 `static/app.js`의 `onmessage` 양쪽을 같이 고칠 것.
- **mode 파라미터** (`/ws/{project}?mode=`): `resume`/`continue`/`attach`는 라이브 세션이 있으면 전부 재접속이고, 없을 때만 아래처럼 갈린다.
  - `new` — 라이브 세션이 있으면 종료 후 새 `claude` 기동
  - `resume` — `claude --resume` (터미널 안에서 전체 세션 목록 중 선택; "이어하기" 버튼)
  - `continue` — `claude --continue` (최근 세션 자동 이어하기; 비정상 단절 자동 재연결에 사용)
  - `attach` — 새 세션
  - resume/continue인데 세션 기록(`~/.claude/projects/<munged-cwd>/*.jsonl`)이 없으면 새 세션으로 폴백한다.
- **원격(ssh) 프로젝트:** `projects.json` 항목에 `"ssh": "user@호스트"`를 넣으면 그 호스트에서 claude를 실행한다 (VPN 내 다른 머신 등록용). 전제: 서버→원격 키 기반 ssh 접속 + 원격에 claude CLI 설치. 스폰은 `ssh -t <host> 'exec bash -lc "cd <path> && exec claude ..."'` — 로그인 셸로 감싸는 이유는 비대화식 ssh PATH에 `~/.local/bin`이 없어서다. PTY는 로컬이므로 resize/시그널/grace/replay가 전부 그대로 동작하고, ssh가 끊기면 원격 claude는 SIGHUP으로 정리된다. path는 원격 기준 경로라 로컬 존재 검증을 건너뛰며, resume/continue 폴백 판단은 `remote_has_history()`(BatchMode ssh로 원격 `~/.claude/projects/` 확인, 실패 시 새 세션 폴백)가 담당한다. `/api/projects`의 `has_history`는 원격이면 낙관적으로 항상 true.
- **셸 세션** (`/ws/{project}?shell=1`, UI의 "셸" 버튼): claude 대신 프로젝트 cwd에서 `bash -l`을 띄운다 (ssh 프로젝트면 원격 셸). 세션 키가 `<name>#shell`로 분리되어 같은 프로젝트의 claude 세션과 **동시에** 유지되고, grace/replay/재연결도 동일하게 동작한다. 셸에는 이어하기 개념이 없어 mode는 라이브 재접속 아니면 새 기동으로만 갈린다. `/api/projects`의 `shell_live`가 셸 라이브 여부(UI의 SHELL 배지).
- **연결 해제 정책:** **해제 → 60초(grace_seconds) 동안 프로세스 무손상 유지 → SIGTERM → 10초 → SIGKILL(프로세스 그룹 전체)** 이다. 재연결 시 grace 타이머가 취소되고 최근 출력 버퍼(최대 256KB)를 replay한 뒤, 클라이언트가 보내는 resize가 SIGWINCH 재렌더링을 유발해 화면이 복원된다.
- **다중 접속:** 같은 프로젝트에 새 클라이언트가 붙으면 기존 WS를 close code 4000으로 끊고 교체한다. 클라이언트는 4000/4401(인증 필요)/4404(화이트리스트 외)를 받으면 자동 재연결하지 않는다. 그 외 비정상 단절은 `mode=continue`로 최대 20회 백오프 재연결한다.
- **패스워드 인증 (선택):** `projects.json`에 `"password_sha256"` 필드를 넣으면 활성화되고, 없으면 무인증(기존 동작). 해시 생성: `echo -n '패스워드' | sha256sum`. `POST /api/login` 성공 시 서버가 랜덤 토큰(`secrets.token_urlsafe`)을 HttpOnly 쿠키(`wterm_token`)로 발급하며, 토큰은 서버 메모리에만 보관되어 재시작 시 전부 무효화된다(재로그인 필요). 미인증 접근은 `/api/projects` → 401, WS → accept 직후 close 4401 (accept 전에 close하면 close code가 클라이언트에 전달되지 않아서다). 클라이언트(`static/app.js`)는 401/4401을 받으면 로그인 오버레이를 띄운다.
- **로깅:** ERROR 레벨 이상만 `logs/wterm.log`에 기록한다 (`setup_logging`). 자정마다 로테이션하고 직전 파일 1개만 보관(`backupCount=1`), uvicorn access 로그는 비활성화. 그 외 영구 로그 없음.
- **유닉스 소켓 리스닝 (선택):** `projects.json`에 `"uds": "<소켓 경로>"`를 넣으면 host/port TCP 바인딩 대신 해당 경로의 유닉스 도메인 소켓으로 리슨한다 (`server/main.py`의 `main()`). 도커로 뜬 리버스 프록시(Caddy 등)가 호스트와 다른 Docker 네트워크에 있어 TCP 포트로 붙기 어려울 때, 소켓 파일의 부모 디렉터리를 프록시 컨테이너에 바인드 마운트해서 `reverse_proxy unix//경로`로 연결하는 용도 — 인바운드 포트를 아예 열지 않아도 된다. uvicorn이 bind 직후 소켓 파일을 자동으로 `chmod 666`하므로 프록시 컨테이너가 다른 UID로 돌아도 접근 가능하다. 기동 시 동일 경로에 이전 비정상 종료로 남은 소켓 파일이 있으면 지우고 새로 bind한다(싱글턴은 `start.sh`의 PID 파일이 보장).

## 주의사항

- **임의 명령 실행 가능한 서버다.** 반드시 내부 IP(기본 127.0.0.1)에만 바인딩하고 앞단 1차 방어 수단이 반드시 필요하다. 패스워드 인증은 2차 잠금일 뿐이며(전송이 평문 HTTP), `projects.json`의 host를 0.0.0.0으로 바꾸지 말 것. 가능하면 TCP 대신 위 `uds` 옵션으로 리슨해 인바운드 포트 자체를 없애는 쪽이 더 안전하다.
- 이 저장소 자체가 화이트리스트에 들어 있으므로, 웹 UI에서 이 서버 코드를 claude로 수정하는 셀프호스팅 개발이 가능하다. 단 이 경우 서버 재시작 전까지는 코드 변경이 반영되지 않는다(uvicorn reload 미사용).
- 개발 중 `pgrep claude`로 프로세스를 정리할 때, 이 시스템이 띄운 자식이 아닌 **사용자가 직접 실행 중인 claude 세션이 함께 잡힐 수 있으니 pid를 확인하고 죽일 것**.
- 테스트는 별도 프레임워크 없이 WS 스모크 테스트로 검증했다: 새 세션 기동 → ANSI 출력 수신 → input/resize 주입 → 해제 후 재접속 시 버퍼 replay → 서버 종료 시 자식 프로세스 회수. 세션 로직을 고치면 같은 시나리오를 다시 돌려볼 것 (스크립트 예시는 uvicorn[standard]에 포함된 `websockets` 클라이언트로 작성 가능).
- Safari/WebKit 한글 IME 조합 버그가 존재한다. 타 브라우저에서는 정상동작하니 타 브라우저 이용 권장.

## TODO
- codex 등 기타 ai 연계
- 패스워드 고도화
