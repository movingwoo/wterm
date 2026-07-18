"""화이트리스트 및 서버 설정 로더.

projects.json 예시:
{
  "host": "127.0.0.1",
  "port": 8877,
  "uds": "/app/wterm/run/wterm.sock",
  "grace_seconds": 60,
  "password_sha256": "<echo -n '패스워드' | sha256sum 결과. 없으면 무인증>",
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
    password_sha256: str | None = None  # 없으면 인증 비활성화
    projects: list[Project] = field(default_factory=list)

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
    password_sha256 = raw.get("password_sha256")
    return Config(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 8877)),
        uds=raw.get("uds") or None,
        grace_seconds=int(raw.get("grace_seconds", 60)),
        password_sha256=password_sha256.strip().lower() if password_sha256 else None,
        projects=projects,
    )
