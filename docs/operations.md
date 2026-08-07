# 운영 — 기동, 재시작, 부팅 시 자동 기동

## 기동과 종료

```bash
./start.sh   # 기동 (기동 실패 시 로그와 함께 알림)
./stop.sh    # 정상 종료 (자식 Claude/Codex/셸 세션까지 정리)
```

감시자(launchd/systemd)를 설치했다면 `start.sh`가 **감시자를 통해** 띄우므로 root가
필요하다 — `./stop.sh && sudo ./start.sh`. 직접 띄우면 감시자 밖 프로세스가 되어
크래시해도 되살아나지 않고 환경도 유닛과 달라지기 때문에, 조용히 그쪽으로 빠지지 않고
명령을 안내하고 멈춘다.

## pid 파일은 잠금이기도 하다

`logs/wterm.pid`는 **서버가 직접 쓴다.** 어떤 방식으로 띄우든 같은 파일이 나오므로
`stop.sh`와 인증서 리로드 훅이 동일하게 동작한다.

동시에 **중복 기동을 막는 잠금**이다. 이미 서버가 떠 있으면 두 번째 프로세스는 (포트가
달라도) 거부되고 종료 코드 3으로 끝난다.

```
wterm.pid: 이미 서버가 실행 중입니다 (pid 1589) — 기동을 중단합니다. 먼저 ./stop.sh 로 내리세요
```

## 부팅 시 자동 기동 (macOS)

```bash
sudo ./scripts/install-launchd.sh              # 설치
sudo ./scripts/install-launchd.sh --uninstall  # 제거
```

LaunchAgent가 아니라 **LaunchDaemon + `UserName`**을 설치한다. LaunchAgent는 "로그인
시"에만 뜨기 때문에, 재부팅 후 물리적으로 로그인해야 서버가 올라온다면 원격 접속
용도로 쓸모가 없다. LaunchDaemon은 부팅 시 뜨고, `UserName` 지정으로 root가 아닌 해당
사용자 권한으로 실행된다.

로그인 셸을 거치지 않아 `PATH`가 최소값이 되므로, 설치 스크립트가 `~/.local/bin`과
`/opt/homebrew/bin`을 명시적으로 넣어준다(서버가 `claude`/`codex`를 직접 spawn하기
때문). 재부팅 후에는 실제로 세션이 기동되는지 한 번 확인할 것.

```bash
./stop.sh && sudo ./start.sh                          # 재시작 (코드 수정 후 필수)
sudo launchctl bootout    system/com.wterm.server     # 등록 해제 (부팅 시에도 안 뜸)
sudo launchctl print      system/com.wterm.server     # 상태
```

재시작에 `launchctl kickstart -k`를 쓰지 말 것. `-k`는 실행 중인 인스턴스를 죽이는
것이라 `SIGTERM` 경로를 타지 않고, PTY 세션 회수가 건너뛰어진다 — 세션들은 자기
프로세스 그룹에 있어서 **서버만 사라지고 살아남는다.** `stop.sh`는 root 없이도
동작한다(정상 종료라 launchd가 되살리지 않는다).

`KeepAlive`는 비정상 종료일 때만 재기동하도록 설정되어 있어, `./stop.sh`로 내린 서버를
launchd가 곧바로 되살리지 않는다. 단 `stop.sh`가 20초 폴백으로 SIGKILL까지 갔다면
비정상 종료로 보여 되살아난다 — 이때는 스크립트가 완전히 내리는 명령을 함께 출력한다.

## 부팅 시 자동 기동 (Linux)

```bash
sudo ./scripts/install-systemd.sh              # 설치
sudo ./scripts/install-systemd.sh --uninstall  # 제거
```

launchd 쪽과 같은 모양이다: **시스템 유닛 + `User=`**. 사용자 유닛(`systemd --user`)은
그 사용자의 로그인 세션이 있어야 뜨고, linger를 켜서 우회하더라도 설정이 계정 상태에
숨어 있어 "왜 안 떴는지"를 추적하기 어렵다.

```bash
./stop.sh && sudo ./start.sh             # 재시작 (코드 수정 후 필수)
sudo systemctl restart wterm             # 같음 — systemd의 stop은 SIGTERM이라 정상 종료다
sudo systemctl stop    wterm             # 중지
systemctl status       wterm             # 상태
systemctl list-timers  wterm-certrenew.timer
```

`Restart=on-failure`가 launchd의 `KeepAlive{SuccessfulExit:false}`와 같은 역할을 한다.
인증서 갱신은 `wterm-certrenew.timer`가 담당한다(→ [docs/https.md](https.md)).

> 리눅스 경로는 CI가 매 푸시마다 진짜 리눅스 VM에서 설치 → 기동 → `stop.sh` 후
> 되살아나지 않음 → SIGKILL 후 되살아남 → 제거까지 왕복시킨다. **재부팅 후 자동 기동,
> systemd 240 미만의 `append:` 폴백, 우분투가 아닌 배포판**은 러너에서 재현할 수 없어
> 실기 확인 대상으로 남아 있다.
