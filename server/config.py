"""화이트리스트 및 서버 설정 로더.

projects.json 예시:
{
  "host": "127.0.0.1",
  "port": 8877,
  "uds": "/app/wterm/run/wterm.sock",
  "grace_seconds": 60,
  "idle_seconds": 0,
  "password_hash": "<argon2id 해시. 없으면 무인증>",
  "allowed_origins": ["https://wterm.example.com:8443"],
  "tls_certfile": "/Users/me/.wterm/fullchain.pem",
  "tls_keyfile": "/Users/me/.wterm/key.pem",
  "projects": [
    {
      "name": "wterm",
      "path": "/app/wterm",
      "args": {
        "claude": ["--model", "your-claude-model"],
        "codex": ["--model", "your-codex-model"]
      },
      "env": {"WTERM_PROJECT": "wterm"}
    },
    {"name": "원격예시", "path": "/home/user/foo", "ssh": "user@100.x.x.x"}
  ]
}

"ssh"가 있으면 해당 호스트에서 `ssh -t`로 claude를 실행한다 (키 기반 접속 권장,
원격에 claude CLI 설치 필요). path는 원격 머신 기준 경로라 로컬 존재 검증을 건너뛴다.

"idle_seconds"가 양수면 그 시간 동안 **양방향 트래픽이 전혀 없는** 세션을 종료한다
(0 = 끔, 기본값). grace_seconds는 연결이 끊겨야 도는 타이머라 탭을 열어둔 채 잊은
세션을 잡지 못하는데, 이것이 그 경우를 덮는다. 활동으로 세는 것은 클라이언트 입력과
PTY 출력 양쪽이라, 사람 없이 오래 도는 자동 실행은 출력이 있는 한 종료되지 않는다.

"uds"가 있으면 host/port 대신 해당 경로의 유닉스 도메인 소켓으로 리슨한다 (TCP
포트를 아예 열지 않는다). 리버스 프록시 컨테이너가 다른 Docker 네트워크에 있어
호스트 포트로 접근하기 어려울 때, 소켓 파일이 있는 디렉터리를 컨테이너에
바인드 마운트해서 쓰는 용도. 이 구성에서는 앞단이 TLS를 담당한다고 보고 쿠키에
Secure를 붙인다 — 앞단을 **평문 http로** 서비스한다면 프록시가
`X-Forwarded-Proto: http`를 붙여야 한다 (유닉스 소켓에는 클라이언트 주소가 없어
uvicorn의 scheme 보정이 동작하지 않으므로 서버가 이 헤더를 직접 본다).

"allowed_origins"는 보통 비워둔다 — 비어 있으면 "Origin의 호스트가 Host 헤더와
같을 것"을 요구하며, 이것이 일반적인 구성에서 옳은 규칙이다. https로 들어온
요청에는 오리진의 스킴도 https일 것을 함께 요구한다 (표준 포트에서는 http와
https의 netloc이 같아 호스트만 봐서는 평문 오리진이 통과한다). 앞단 프록시가
Host를 바꿔 쓰는 등 그 규칙이 안 맞는 구성에서만 접속에 쓰는 오리진을 스킴/포트까지
포함해 명시한다 (예: "https://wterm.example.com:8443").

"tls_certfile"/"tls_keyfile"이 둘 다 있으면 앞단 리버스 프록시 없이 서버가 직접
HTTPS로 리슨한다 (uds 사용 시에는 무시됨). 인증서를 발급/갱신하는 것은 이 서버의
역할이 아니다 — 파일을 읽기만 하며, 갱신 후 SIGHUP을 받으면 재시작 없이 다시
읽는다. 발급은 scripts/cert-setup.sh 참고.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "projects.json"
PROJECT_ARG_AGENTS = frozenset(("claude", "codex"))
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass
class Project:
    name: str
    path: str
    ssh: str | None = None  # "user@host" — 지정 시 원격 호스트에서 claude 실행
    # CLI 전역 옵션. resume/continue 서브커맨드보다 앞에 놓인다.
    args: dict[str, list[str]] = field(default_factory=dict)
    # 로컬/원격과 에이전트/셸에 똑같이 적용하는 프로젝트 환경.
    env: dict[str, str] = field(default_factory=dict)

    def args_for(self, agent: str) -> list[str]:
        """세션이 자기 사본을 갖도록 새 리스트를 돌려준다."""
        return list(self.args.get(agent, ()))


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8877
    uds: str | None = None  # 설정 시 host/port 대신 유닉스 소켓으로 리슨
    grace_seconds: int = 60
    idle_seconds: int = 0  # 0이면 유휴 종료 없음. 양수면 그만큼 조용한 세션을 종료
    password_hash: str | None = None  # argon2id 해시. 없으면 인증 비활성화
    # 허용 오리진 화이트리스트. 비어 있으면 Origin 호스트 == Host 헤더로 판정한다.
    allowed_origins: list[str] = field(default_factory=list)
    tls_certfile: str | None = None  # 풀체인 PEM. keyfile과 함께 있을 때만 HTTPS
    tls_keyfile: str | None = None  # 개인키 PEM
    projects: list[Project] = field(default_factory=list)

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_certfile and self.tls_keyfile)

    def find_project(self, name: str) -> Project | None:
        for p in self.projects:
            if p.name == name:
                return p
        return None


def _project_args(raw: object, project_name: str) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"프로젝트 {project_name!r}: args는 객체여야 함")
    unknown = set(raw) - PROJECT_ARG_AGENTS
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise ValueError(f"프로젝트 {project_name!r}: 지원하지 않는 args 대상: {names}")
    result: dict[str, list[str]] = {}
    for agent, values in raw.items():
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(
                f"프로젝트 {project_name!r}: args.{agent}는 문자열 배열이어야 함"
            )
        if any("\0" in value for value in values):
            raise ValueError(f"프로젝트 {project_name!r}: 실행 인자에 NUL을 넣을 수 없음")
        if "--" in values:
            # 이 뒤의 강제 알림 옵션과 resume/continue를 위치 인자로 바꿔 버린다.
            raise ValueError(f"프로젝트 {project_name!r}: args에 옵션 종결자 --를 넣을 수 없음")
        result[agent] = list(values)
    return result


def _project_env(raw: object, project_name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"프로젝트 {project_name!r}: env는 객체여야 함")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or ENV_NAME_RE.fullmatch(key) is None:
            raise ValueError(f"프로젝트 {project_name!r}: 잘못된 환경변수 이름: {key!r}")
        if key == "TERM":
            # PTY와 xterm.js가 합의한 값이라 프로젝트마다 바뀌면 렌더링이 깨진다.
            raise ValueError(f"프로젝트 {project_name!r}: TERM은 W-Term이 관리함")
        if not isinstance(value, str):
            raise ValueError(f"프로젝트 {project_name!r}: env.{key}는 문자열이어야 함")
        if "\0" in value:
            raise ValueError(f"프로젝트 {project_name!r}: env.{key}에 NUL을 넣을 수 없음")
        result[key] = value
    return result


def load_config() -> Config:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    projects = []
    for item in raw.get("projects", []):
        name = item["name"]
        ssh = item.get("ssh") or None
        args = _project_args(item.get("args"), name)
        env = _project_env(item.get("env"), name)
        if ssh is not None:
            # 원격 경로는 로컬에 없으므로 존재 검증 없이 그대로 사용
            projects.append(
                Project(name=name, path=item["path"], ssh=ssh, args=args, env=env)
            )
            continue
        path = Path(item["path"]).resolve()
        if not path.is_dir():
            print(f"[config] 경고: 디렉터리가 없어 제외함: {path}")
            continue
        projects.append(Project(name=name, path=str(path), args=args, env=env))
    password_hash = raw.get("password_hash")
    # 비교는 소문자 기준. 브라우저가 보내는 Origin에는 끝 슬래시가 없으므로 떼어 맞춘다.
    allowed_origins = [
        o.strip().rstrip("/").lower()
        for o in raw.get("allowed_origins", [])
        if isinstance(o, str) and o.strip()
    ]
    certfile = raw.get("tls_certfile") or None
    keyfile = raw.get("tls_keyfile") or None
    if bool(certfile) != bool(keyfile):
        # 한쪽만 있으면 HTTPS를 켤 수 없다. 조용히 평문으로 뜨면 눈치채기 어려우므로 경고.
        print("[config] 경고: tls_certfile과 tls_keyfile은 함께 지정해야 함. HTTP로 기동함")
        certfile = keyfile = None
    return Config(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 8877)),
        uds=raw.get("uds") or None,
        grace_seconds=int(raw.get("grace_seconds", 60)),
        idle_seconds=max(0, int(raw.get("idle_seconds", 0))),
        password_hash=password_hash.strip() if password_hash else None,
        allowed_origins=allowed_origins,
        tls_certfile=certfile,
        tls_keyfile=keyfile,
        projects=projects,
    )
