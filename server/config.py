"""화이트리스트 및 서버 설정 로더.

projects.json 예시:
{
  "host": "127.0.0.1",
  "port": 8877,
  "uds": "/app/wterm/run/wterm.sock",
  "grace_seconds": 60,
  "password_hash": "<argon2id 해시. 없으면 무인증>",
  "tls_certfile": "/Users/me/.wterm/fullchain.pem",
  "tls_keyfile": "/Users/me/.wterm/key.pem",
  "projects": [
    {"name": "wterm", "path": "/app/wterm"},
    {"name": "원격예시", "path": "/home/user/foo", "ssh": "user@100.x.x.x"}
  ]
}

"ssh"가 있으면 해당 호스트에서 `ssh -t`로 claude를 실행한다 (키 기반 접속 권장,
원격에 claude CLI 설치 필요). path는 원격 머신 기준 경로라 로컬 존재 검증을 건너뛴다.

"uds"가 있으면 host/port 대신 해당 경로의 유닉스 도메인 소켓으로 리슨한다 (TCP
포트를 아예 열지 않는다). 리버스 프록시 컨테이너가 다른 Docker 네트워크에 있어
호스트 포트로 접근하기 어려울 때, 소켓 파일이 있는 디렉터리를 컨테이너에
바인드 마운트해서 쓰는 용도.

"tls_certfile"/"tls_keyfile"이 둘 다 있으면 앞단 리버스 프록시 없이 서버가 직접
HTTPS로 리슨한다 (uds 사용 시에는 무시됨). 인증서를 발급/갱신하는 것은 이 서버의
역할이 아니다 — 파일을 읽기만 하며, 갱신 후 SIGHUP을 받으면 재시작 없이 다시
읽는다. 발급은 scripts/cert-setup.sh 참고.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "projects.json"


@dataclass
class Project:
    name: str
    path: str
    ssh: str | None = None  # "user@host" — 지정 시 원격 호스트에서 claude 실행


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8877
    uds: str | None = None  # 설정 시 host/port 대신 유닉스 소켓으로 리슨
    grace_seconds: int = 60
    password_hash: str | None = None  # argon2id 해시. 없으면 인증 비활성화
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


def load_config() -> Config:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    projects = []
    for item in raw.get("projects", []):
        ssh = item.get("ssh") or None
        if ssh is not None:
            # 원격 경로는 로컬에 없으므로 존재 검증 없이 그대로 사용
            projects.append(Project(name=item["name"], path=item["path"], ssh=ssh))
            continue
        path = Path(item["path"]).resolve()
        if not path.is_dir():
            print(f"[config] 경고: 디렉터리가 없어 제외함: {path}")
            continue
        projects.append(Project(name=item["name"], path=str(path)))
    password_hash = raw.get("password_hash")
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
        password_hash=password_hash.strip() if password_hash else None,
        tls_certfile=certfile,
        tls_keyfile=keyfile,
        projects=projects,
    )
