"""화이트리스트 및 서버 설정 로더.

projects.json 예시:
{
  "host": "127.0.0.1",
  "port": 8877,
  "uds": "/app/wterm/run/wterm.sock",
  "grace_seconds": 60,
  "idle_seconds": 0,
  "password_hash": null,
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
import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

CONFIG_PATH = Path(__file__).resolve().parent.parent / "projects.json"
PROJECT_ARG_AGENTS = frozenset(("claude", "codex"))
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
PORT_MAX = 65535
GRACE_SECONDS_MAX = 24 * 60 * 60
IDLE_SECONDS_MAX = 365 * 24 * 60 * 60


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
    # loopback 밖의 무인증 또는 평문 TCP를 정말 의도한 경우에만 켜는 위험 승인.
    allow_insecure_tcp: bool = False
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


def _integer(
    raw: dict[str, object], name: str, default: int, minimum: int, maximum: int
) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}는 정수여야 함")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}는 {minimum}..{maximum} 범위여야 함")
    return value


def _optional_string(raw: dict[str, object], name: str) -> str | None:
    value = raw.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name}는 문자열이어야 함")
    value = value.strip()
    return value or None


def _password_hash(raw: dict[str, object]) -> str | None:
    value = _optional_string(raw, "password_hash")
    if value is None:
        return None
    if not value.startswith("$argon2id$"):
        raise ValueError("password_hash는 유효한 Argon2id 해시여야 함")
    try:
        # 고정 문자열이 맞을 가능성은 무시할 만큼 작다. mismatch는 전체 인코딩과
        # 파라미터를 정상적으로 해석했다는 뜻이고, decode/버전 오류만 거부한다.
        PasswordHasher().verify(value, "wterm-config-validation")
    except VerifyMismatchError:
        pass
    except (InvalidHashError, VerificationError) as exc:
        raise ValueError("password_hash는 유효한 Argon2id 해시여야 함") from exc
    return value


def _allowed_origins(raw: dict[str, object]) -> list[str]:
    values = raw.get("allowed_origins", [])
    if not isinstance(values, list):
        raise ValueError("allowed_origins는 문자열 배열이어야 함")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("allowed_origins는 비어 있지 않은 문자열 배열이어야 함")
        origin = value.strip()
        parts = urlsplit(origin)
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("allowed_origins에 유효하지 않은 origin이 있음") from exc
        if (
            parts.scheme.lower() not in ("http", "https")
            or parts.hostname is None
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or any(ch.isspace() for ch in parts.netloc)
        ):
            raise ValueError("allowed_origins에는 path/query/fragment 없는 완전한 origin만 허용")
        # port 속성을 읽는 것 자체가 범위 검증이다. netloc은 IPv6 괄호를 보존한다.
        _ = port
        normalized = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        if normalized not in result:
            result.append(normalized)
    return result


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config() -> Config:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("projects.json 최상위 값은 객체여야 함")

    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host는 비어 있지 않은 문자열이어야 함")
    host = host.strip()
    port = _integer(raw, "port", 8877, 1, PORT_MAX)
    grace_seconds = _integer(raw, "grace_seconds", 60, 0, GRACE_SECONDS_MAX)
    idle_seconds = _integer(raw, "idle_seconds", 0, 0, IDLE_SECONDS_MAX)
    uds = _optional_string(raw, "uds")
    password_hash = _password_hash(raw)
    allowed_origins = _allowed_origins(raw)
    certfile = _optional_string(raw, "tls_certfile")
    keyfile = _optional_string(raw, "tls_keyfile")
    if bool(certfile) != bool(keyfile):
        raise ValueError("tls_certfile과 tls_keyfile은 함께 지정해야 함")
    allow_insecure_tcp = raw.get("allow_insecure_tcp", False)
    if not isinstance(allow_insecure_tcp, bool):
        raise ValueError("allow_insecure_tcp는 bool이어야 함")

    project_items = raw.get("projects", [])
    if not isinstance(project_items, list):
        raise ValueError("projects는 배열이어야 함")
    projects: list[Project] = []
    names: set[str] = set()
    for index, item in enumerate(project_items):
        if not isinstance(item, dict):
            raise ValueError(f"projects[{index}]는 객체여야 함")
        name_value = item.get("name")
        if not isinstance(name_value, str) or not name_value.strip():
            raise ValueError(f"projects[{index}].name은 비어 있지 않은 문자열이어야 함")
        name = name_value.strip()
        if name in names:
            raise ValueError(f"중복 프로젝트 이름: {name!r}")
        names.add(name)
        path_value = item.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError(f"프로젝트 {name!r}: path는 비어 있지 않은 문자열이어야 함")
        project_path = path_value.strip()
        ssh_value = item.get("ssh")
        if ssh_value is not None and (
            not isinstance(ssh_value, str) or not ssh_value.strip()
        ):
            raise ValueError(f"프로젝트 {name!r}: ssh는 비어 있지 않은 문자열이어야 함")
        ssh = ssh_value.strip() if isinstance(ssh_value, str) else None
        args = _project_args(item.get("args"), name)
        env = _project_env(item.get("env"), name)
        if ssh is not None:
            # 원격 경로는 로컬에 없으므로 존재 검증 없이 그대로 사용
            projects.append(
                Project(name=name, path=project_path, ssh=ssh, args=args, env=env)
            )
            continue
        path = Path(project_path).resolve()
        if not path.is_dir():
            print(f"[config] 경고: 디렉터리가 없어 제외함: {path}")
            continue
        projects.append(Project(name=name, path=str(path), args=args, env=env))

    tls_enabled = bool(certfile and keyfile)
    if uds is None and not _is_loopback_host(host) and not allow_insecure_tcp:
        if password_hash is None:
            raise ValueError(
                "loopback 밖의 TCP bind에는 인증이 필요함; 위험을 감수하려면 "
                "allow_insecure_tcp=true를 명시"
            )
        if not tls_enabled:
            raise ValueError(
                "loopback 밖의 평문 TCP bind는 거부됨; TLS/UDS를 쓰거나 위험을 "
                "감수하려면 allow_insecure_tcp=true를 명시"
            )
    return Config(
        host=host,
        port=port,
        uds=uds,
        grace_seconds=grace_seconds,
        idle_seconds=idle_seconds,
        password_hash=password_hash,
        allowed_origins=allowed_origins,
        tls_certfile=certfile,
        tls_keyfile=keyfile,
        allow_insecure_tcp=allow_insecure_tcp,
        projects=projects,
    )
