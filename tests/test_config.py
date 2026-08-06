"""projects.json 로더. 여기만 프로세스를 띄우지 않고 직접 import해서 본다."""
from __future__ import annotations

import json

import pytest

from server import config as config_mod


@pytest.fixture
def load(tmp_path, monkeypatch):
    """임시 projects.json을 읽는 load_config. 저장소의 실제 설정은 건드리지 않는다."""

    def _load(raw: dict):
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
        return config_mod.load_config()

    return _load


def test_defaults(load):
    cfg = load({})
    assert (cfg.host, cfg.port, cfg.grace_seconds) == ("127.0.0.1", 8877, 60)
    assert cfg.uds is None
    assert cfg.password_hash is None  # 인증 비활성화
    assert cfg.allowed_origins == []
    assert cfg.tls_enabled is False


def test_local_project_must_exist(load, tmp_path):
    """없는 디렉터리는 조용히 빠진다 — 화이트리스트가 곧 접근 제어라서."""
    good = tmp_path / "good"
    good.mkdir()
    cfg = load(
        {
            "projects": [
                {"name": "good", "path": str(good)},
                {"name": "gone", "path": str(tmp_path / "gone")},
            ]
        }
    )
    assert [p.name for p in cfg.projects] == ["good"]
    assert cfg.find_project("gone") is None
    assert cfg.find_project("good").path == str(good.resolve())


def test_ssh_project_skips_local_path_check(load):
    """원격 경로는 로컬에 없는 것이 정상이므로 존재 검증을 하면 안 된다."""
    cfg = load(
        {"projects": [{"name": "remote", "path": "/home/u/x", "ssh": "u@host"}]}
    )
    assert [(p.name, p.path, p.ssh) for p in cfg.projects] == [
        ("remote", "/home/u/x", "u@host")
    ]


def test_tls_needs_both_files(load, capsys):
    """한쪽만 있으면 HTTPS를 켤 수 없다. 조용히 평문으로 뜨면 알아채기 어렵다."""
    cfg = load({"tls_certfile": "/tmp/full.pem"})
    assert cfg.tls_enabled is False
    assert (cfg.tls_certfile, cfg.tls_keyfile) == (None, None)
    assert "경고" in capsys.readouterr().out


def test_tls_enabled_with_both(load):
    cfg = load({"tls_certfile": "/tmp/full.pem", "tls_keyfile": "/tmp/key.pem"})
    assert cfg.tls_enabled is True


def test_allowed_origins_normalized(load):
    """브라우저 Origin에는 끝 슬래시가 없고 대소문자도 섞여 들어온다."""
    cfg = load(
        {
            "allowed_origins": [
                " https://WTerm.Example.com:8443/ ",
                "",
                "   ",
                None,
                123,
            ]
        }
    )
    assert cfg.allowed_origins == ["https://wterm.example.com:8443"]


def test_password_hash_stripped(load):
    cfg = load({"password_hash": "  $argon2id$v=19$m=8,t=1,p=1$abc$def  "})
    assert cfg.password_hash == "$argon2id$v=19$m=8,t=1,p=1$abc$def"


def test_example_config_is_valid_json():
    """README가 가리키는 예시 파일이 깨진 채로 커밋되지 않게."""
    from server.config import CONFIG_PATH

    example = CONFIG_PATH.parent / "projects.example.json"
    raw = json.loads(example.read_text(encoding="utf-8"))
    assert isinstance(raw.get("projects"), list) and raw["projects"]
    for item in raw["projects"]:
        assert "name" in item and "path" in item
