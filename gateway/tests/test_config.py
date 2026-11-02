"""Tests for typed configuration and secrets loading."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from pdl_gateway.config import (
    ConfigError,
    GatewaySecrets,
    check_secrets,
    load_config,
    load_secrets,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "gateway.example.yaml"
SECRETS_EXAMPLE = Path(__file__).resolve().parents[1] / "secrets.example.yaml"


def _write_variant(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "g.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_loads_example() -> None:
    cfg = load_config(EXAMPLE)
    assert cfg.issuer_url == "https://gateway.lab"
    assert [b.name for b in cfg.backends] == ["homestudio", "automations"]
    assert cfg.backends[1].allow_tools == (
        "list_devices",
        "get_device",
        "list_programs",
        "get_program",
        "get_program_logs",
        "get_usb_power",
        "recent_history",
    )
    assert cfg.interactive_scopes == ("homestudio:read", "automations:read")
    assert cfg.grant("personal-assistant").kind == "headless"
    assert cfg.grant("missing") is None
    assert cfg.access_token_ttl_s == 3600
    assert cfg.refresh_token_ttl_s == 43200
    assert cfg.auth_code_ttl_s == 300
    assert cfg.tools_cache_ttl_s == 60


def test_secret_header_defaults_when_secret_env_given(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        del raw["backends"][0]["secret_header"]

    cfg = load_config(_write_variant(tmp_path, mutate))
    assert cfg.backends[0].secret_header == "X-Gateway-Secret"
    assert cfg.backends[1].secret_header is None


def test_backend_scope_must_be_declared(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["backends"][0]["scope"] = "nope:read"

    with pytest.raises(ConfigError, match="nope:read"):
        load_config(_write_variant(tmp_path, mutate))


def test_grant_scope_must_be_declared(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["grants"][0]["scopes"].append("bank:write")

    with pytest.raises(ConfigError, match="bank:write"):
        load_config(_write_variant(tmp_path, mutate))


def test_interactive_grant_must_exist(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["grants"] = [g for g in raw["grants"] if g["name"] != "interactive-default"]

    with pytest.raises(ConfigError, match="interactive-default"):
        load_config(_write_variant(tmp_path, mutate))


def test_interactive_grant_must_be_interactive_kind(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["interactive_grant"] = "personal-assistant"

    with pytest.raises(ConfigError, match="personal-assistant"):
        load_config(_write_variant(tmp_path, mutate))


def test_duplicate_backend_names_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["backends"][1]["name"] = "homestudio"

    with pytest.raises(ConfigError, match="homestudio"):
        load_config(_write_variant(tmp_path, mutate))


def test_backend_needs_one_auth_method(tmp_path: Path) -> None:
    def neither(raw: dict[str, Any]) -> None:
        del raw["backends"][0]["secret_env"]

    def both(raw: dict[str, Any]) -> None:
        raw["backends"][0]["bearer_env"] = "SOME_TOKEN"

    with pytest.raises(ConfigError, match="homestudio"):
        load_config(_write_variant(tmp_path, neither))
    with pytest.raises(ConfigError, match="homestudio"):
        load_config(_write_variant(tmp_path, both))


def test_missing_required_key_is_config_error(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        del raw["issuer_url"]

    with pytest.raises(ConfigError, match="issuer_url"):
        load_config(_write_variant(tmp_path, mutate))


def test_non_mapping_document_is_config_error(tmp_path: Path) -> None:
    path = tmp_path / "g.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_grant_kind_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["grants"][0]["kind"] = "robot"

    with pytest.raises(ConfigError, match="robot"):
        load_config(_write_variant(tmp_path, mutate))


def test_secrets_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        yaml.safe_dump(
            {"approval_passphrase": "hash-a", "client_secrets": {"personal-assistant": "hash-b"}}
        ),
        encoding="utf-8",
    )
    secrets = load_secrets(path)
    assert secrets.approval_passphrase_hash == "hash-a"
    assert dict(secrets.client_secret_hashes) == {"personal-assistant": "hash-b"}


def test_secrets_example_loads() -> None:
    secrets = load_secrets(SECRETS_EXAMPLE)
    assert "personal-assistant" in secrets.client_secret_hashes


def test_secrets_missing_key_is_config_error(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text("client_secrets: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="approval_passphrase"):
        load_secrets(path)


def test_check_secrets_requires_headless_hash() -> None:
    cfg = load_config(EXAMPLE)
    missing = GatewaySecrets(approval_passphrase_hash="h", client_secret_hashes={})
    with pytest.raises(ConfigError, match="personal-assistant"):
        check_secrets(cfg, missing)
    ok = GatewaySecrets(
        approval_passphrase_hash="h", client_secret_hashes={"personal-assistant": "x"}
    )
    check_secrets(cfg, ok)


def test_unreadable_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "absent.yaml")


def test_invalid_value_is_config_error(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["listen_port"] = "not-a-port"

    with pytest.raises(ConfigError, match="invalid value"):
        load_config(_write_variant(tmp_path, mutate))


def test_client_secrets_must_be_mapping(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text("approval_passphrase: h\nclient_secrets: [a, b]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="client_secrets"):
        load_secrets(path)


@pytest.mark.parametrize("key", ["secret_env", "bearer_env"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_null_or_empty_auth_key_rejected(tmp_path: Path, key: str, value: str | None) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["backends"][0][key] = value

    with pytest.raises(ConfigError, match=key):
        load_config(_write_variant(tmp_path, mutate))


def test_unknown_backend_key_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["backends"][1]["allow_tool"] = ["list_devices"]

    with pytest.raises(ConfigError, match=r"allow_tool.*automations"):
        load_config(_write_variant(tmp_path, mutate))


def test_unknown_grant_key_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["grants"][0]["scope"] = ["bank:read"]

    with pytest.raises(ConfigError, match=r"'scope'.*personal-assistant"):
        load_config(_write_variant(tmp_path, mutate))


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["listen_prt"] = 1

    with pytest.raises(ConfigError, match=r"listen_prt.*top level"):
        load_config(_write_variant(tmp_path, mutate))


@pytest.mark.parametrize("value", [None, "list_devices"])
def test_allow_tools_must_be_list(tmp_path: Path, value: object) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["backends"][1]["allow_tools"] = value

    with pytest.raises(ConfigError, match="allow_tools"):
        load_config(_write_variant(tmp_path, mutate))


def test_empty_allow_tools_means_no_tools(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["backends"][1]["allow_tools"] = []

    assert load_config(_write_variant(tmp_path, mutate)).backends[1].allow_tools == ()


def test_duplicate_grant_names_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict[str, Any]) -> None:
        raw["grants"].append(dict(raw["grants"][0]))

    with pytest.raises(ConfigError, match="personal-assistant"):
        load_config(_write_variant(tmp_path, mutate))


@pytest.mark.parametrize(
    "body",
    [
        "approval_passphrase:\nclient_secrets: {}\n",
        "approval_passphrase: ''\nclient_secrets: {}\n",
        "approval_passphrase: h\nclient_secrets: {pa: }\n",
        "approval_passphrase: h\nclient_secrets: {pa: ''}\n",
    ],
)
def test_null_or_empty_secret_hashes_rejected(tmp_path: Path, body: str) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty"):
        load_secrets(path)
