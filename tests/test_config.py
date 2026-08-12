"""projects.json 로더. 여기만 프로세스를 띄우지 않고 직접 import해서 본다."""
from __future__ import annotations

import json

import pytest
from argon2 import PasswordHasher

from server import config as config_mod


VALID_HASH = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("test")


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


def test_project_args_and_environment_are_validated_and_copied(load, tmp_path):
    path = tmp_path / "configured"
    path.mkdir()
    cfg = load(
        {
            "projects": [
                {
                    "name": "configured",
                    "path": str(path),
                    "args": {
                        "claude": ["--model", "sonnet"],
                        "codex": ["--model", "gpt-5.4"],
                    },
                    "env": {"PROJECT_KIND": "configured", "EMPTY": ""},
                }
            ]
        }
    )
    project = cfg.projects[0]
    assert project.args == {
        "claude": ["--model", "sonnet"],
        "codex": ["--model", "gpt-5.4"],
    }
    assert project.env == {"PROJECT_KIND": "configured", "EMPTY": ""}

    own_args = project.args_for("claude")
    own_args.append("--unexpected")
    assert project.args_for("claude") == ["--model", "sonnet"]
    assert project.args_for("shell") == []


@pytest.mark.parametrize(
    "runtime",
    [
        {"args": []},
        {"args": {"shell": []}},
        {"args": {"claude": "--model sonnet"}},
        {"args": {"claude": ["--model", 3]}},
        {"args": {"claude": ["bad\0arg"]}},
        {"args": {"codex": ["--"]}},
        {"env": []},
        {"env": {"BAD-NAME": "value"}},
        {"env": {"TERM": "dumb"}},
        {"env": {"COUNT": 3}},
        {"env": {"VALUE": "bad\0value"}},
    ],
)
def test_invalid_project_runtime_config_fails_loudly(load, tmp_path, runtime):
    path = tmp_path / "configured"
    path.mkdir()
    project = {"name": "configured", "path": str(path), **runtime}
    with pytest.raises(ValueError, match="프로젝트 'configured'"):
        load({"projects": [project]})


def test_tls_needs_both_files(load):
    """한쪽만 있으면 평문으로 폴백하지 않고 기동 자체를 거부한다."""
    with pytest.raises(ValueError, match="함께 지정"):
        load({"tls_certfile": "/tmp/full.pem"})


def test_tls_enabled_with_both(load):
    cfg = load({"tls_certfile": "/tmp/full.pem", "tls_keyfile": "/tmp/key.pem"})
    assert cfg.tls_enabled is True


def test_allowed_origins_normalized(load):
    """origin의 스킴/호스트 대소문자는 정규화한다."""
    cfg = load(
        {
            "allowed_origins": [
                " https://WTerm.Example.com:8443 ",
                "https://wterm.example.com:8443",
            ]
        }
    )
    assert cfg.allowed_origins == ["https://wterm.example.com:8443"]


def test_password_hash_stripped(load):
    cfg = load({"password_hash": f"  {VALID_HASH}  "})
    assert cfg.password_hash == VALID_HASH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("port", True),
        ("port", "8877"),
        ("port", 0),
        ("port", 65536),
        ("grace_seconds", False),
        ("grace_seconds", -1),
        ("grace_seconds", config_mod.GRACE_SECONDS_MAX + 1),
        ("idle_seconds", "0"),
        ("idle_seconds", -1),
        ("idle_seconds", config_mod.IDLE_SECONDS_MAX + 1),
    ],
)
def test_numeric_fields_reject_coercion_and_out_of_range(load, field, value):
    with pytest.raises(ValueError, match=field):
        load({field: value})


@pytest.mark.parametrize(
    "projects",
    [
        {},
        ["not-an-object"],
        [{"name": "", "path": "/tmp"}],
        [{"name": 3, "path": "/tmp"}],
        [{"name": "demo", "path": 3}],
        [{"name": "demo", "path": "/tmp", "ssh": 3}],
        [
            {"name": "same", "path": "/missing/a", "ssh": "host"},
            {"name": " same ", "path": "/missing/b", "ssh": "host"},
        ],
    ],
)
def test_project_shape_and_duplicate_names_fail_closed(load, projects):
    with pytest.raises(ValueError):
        load({"projects": projects})


@pytest.mark.parametrize(
    "origins",
    [
        "https://wterm.example.com",
        [""],
        [None],
        ["wterm.example.com"],
        ["ftp://wterm.example.com"],
        ["https://wterm.example.com/"],
        ["https://wterm.example.com/path"],
        ["https://wterm.example.com?q=1"],
        ["https://wterm.example.com#fragment"],
        ["https://user@wterm.example.com"],
        ["https://wterm.example.com:99999"],
    ],
)
def test_allowed_origins_require_complete_origin(load, origins):
    with pytest.raises(ValueError, match="allowed_origins"):
        load({"allowed_origins": origins})


@pytest.mark.parametrize(
    "password_hash",
    [3, "not-a-hash", "$argon2i$v=19$m=8,t=1,p=1$abc$def", "$argon2id$broken"],
)
def test_password_hash_must_be_supported_argon2id(load, password_hash):
    with pytest.raises(ValueError, match="password_hash"):
        load({"password_hash": password_hash})


def test_non_loopback_tcp_requires_auth_and_encryption_or_explicit_override(load):
    with pytest.raises(ValueError, match="인증"):
        load({"host": "0.0.0.0"})
    with pytest.raises(ValueError, match="평문"):
        load({"host": "192.0.2.10", "password_hash": VALID_HASH})

    cfg = load({"host": "0.0.0.0", "allow_insecure_tcp": True})
    assert cfg.allow_insecure_tcp is True


def test_uds_and_authenticated_tls_non_loopback_remain_valid(load):
    assert load({"host": "0.0.0.0", "uds": "/tmp/wterm.sock"}).uds
    cfg = load({
        "host": "0.0.0.0",
        "password_hash": VALID_HASH,
        "tls_certfile": "/secret/cert.pem",
        "tls_keyfile": "/secret/key.pem",
    })
    assert cfg.tls_enabled


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ({"password_hash": "$argon2id$credential-material"}, "credential-material"),
        ({"tls_keyfile": "/secret/private-name.key"}, "/secret/private-name.key"),
        (
            {
                "projects": [{
                    "name": "demo", "path": "/tmp", "ssh": "host",
                    "args": {"claude": ["secret-option", 3]},
                }]
            },
            "secret-option",
        ),
        (
            {
                "projects": [{
                    "name": "demo", "path": "/tmp", "ssh": "host",
                    "env": {"TOKEN": {"secret": "value"}},
                }]
            },
            "value",
        ),
    ],
)
def test_config_errors_do_not_echo_secret_values(load, raw, secret):
    with pytest.raises(ValueError) as exc:
        load(raw)
    assert secret not in str(exc.value)


def test_example_config_is_valid_json_and_schema(load):
    """README가 가리키는 예시 파일이 JSON뿐 아니라 실제 스키마도 통과해야 한다."""
    from server.config import CONFIG_PATH

    example = CONFIG_PATH.parent / "projects.example.json"
    raw = json.loads(example.read_text(encoding="utf-8"))
    assert isinstance(raw.get("projects"), list) and raw["projects"]
    for item in raw["projects"]:
        assert "name" in item and "path" in item
    cfg = load(raw)
    assert cfg.find_project("remote-project") is not None
