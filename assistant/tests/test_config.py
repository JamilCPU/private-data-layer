"""Tests for assistant configuration and secrets loading."""

from pathlib import Path

import pytest

from pdl_assistant.config import AssistantConfig, ConfigError, load_config, load_secrets

REQUIRED = {
    "gateway_url": "https://gateway.example",
    "ollama_url": "http://ollama.example:11434",
    "model": "qwen3.5:27b",
    "client_secret_file": "/run/cs",
    "secrets_file": "/run/s.yaml",
    "db_path": "/data/a.db",
    "public_url": "https://assistant.example",
}


def _write(tmp_path: Path, text: str, name: str = "a.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _yaml(**overrides: object) -> str:
    data = {**REQUIRED, **overrides}
    return "\n".join(f"{k}: {v!r}" for k, v in data.items() if v is not None)


def test_defaults(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _yaml()), {})
    assert cfg.client_id == "personal-assistant"
    assert cfg.listen_host == "0.0.0.0"
    assert cfg.listen_port == 8790
    assert cfg.max_tool_steps == 6
    assert cfg.model_timeout_s == 120
    assert cfg.tool_timeout_s == 60
    assert cfg.session_ttl_s == 43200
    assert cfg.system_prompt_extra == ""


def test_config_is_frozen(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _yaml()), {})
    with pytest.raises(AttributeError):
        cfg.model = "x"  # type: ignore[misc]


def test_env_override_and_cast(tmp_path: Path):
    env = {
        "ASSISTANT_GATEWAY_URL": "http://gw.example",
        "ASSISTANT_MODEL": "other",
        "ASSISTANT_MAX_TOOL_STEPS": "9",
        "ASSISTANT_MODEL_TIMEOUT_S": "1.5",
    }
    cfg = load_config(_write(tmp_path, _yaml()), env)
    assert cfg.gateway_url == "http://gw.example"
    assert cfg.model == "other"
    assert cfg.max_tool_steps == 9
    assert cfg.model_timeout_s == 1.5


def test_env_only_without_file():
    env = {f"ASSISTANT_{k.upper()}": v for k, v in REQUIRED.items()}
    assert isinstance(load_config(None, env), AssistantConfig)


def test_missing_required_names_keys(tmp_path: Path):
    path = _write(tmp_path, _yaml(model=None, db_path=None))
    with pytest.raises(ConfigError, match="db_path, model"):
        load_config(path, {})


def test_unknown_key(tmp_path: Path):
    with pytest.raises(ConfigError, match="bogus"):
        load_config(_write(tmp_path, _yaml() + "\nbogus: 1"), {})


def test_bad_cast(tmp_path: Path):
    with pytest.raises(ConfigError, match="max_tool_steps"):
        load_config(_write(tmp_path, _yaml()), {"ASSISTANT_MAX_TOOL_STEPS": "many"})


def test_bad_yaml_value_type(tmp_path: Path):
    with pytest.raises(ConfigError, match="listen_port"):
        load_config(_write(tmp_path, _yaml(listen_port="abc")), {})


@pytest.mark.parametrize("field", ["gateway_url", "ollama_url", "public_url"])
def test_urls_must_be_http(tmp_path: Path, field: str):
    with pytest.raises(ConfigError, match=field):
        load_config(_write(tmp_path, _yaml(**{field: "ftp://x"})), {})


def test_missing_file_and_bad_documents(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml", {})
    with pytest.raises(ConfigError, match="mapping"):
        load_config(_write(tmp_path, "- a\n- b"), {})
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "a: [unclosed"), {})


def test_non_utf8_file(tmp_path: Path):
    path = tmp_path / "u16.yaml"
    path.write_bytes("model: x".encode("utf-16"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path, {})


def test_load_secrets(tmp_path: Path):
    path = _write(tmp_path, f"api_tokens:\n  phone: h1\ncookie_secret: {'x' * 32}\n", "s.yaml")
    secrets = load_secrets(path)
    assert secrets.api_token_hashes == {"phone": "h1"}
    assert secrets.cookie_secret == "x" * 32


def test_load_secrets_tokens_optional(tmp_path: Path):
    path = _write(tmp_path, f"cookie_secret: {'x' * 40}\n", "s.yaml")
    assert dict(load_secrets(path).api_token_hashes) == {}


@pytest.mark.parametrize(
    "text",
    [
        "api_tokens: {}\n",
        "cookie_secret: short\n",
        f"cookie_secret: {'x' * 32}\nextra: 1\n",
        f"cookie_secret: {'x' * 32}\napi_tokens: [a]\n",
        f"cookie_secret: {'x' * 32}\napi_tokens: {{a: 1}}\n",
    ],
)
def test_load_secrets_invalid(tmp_path: Path, text: str):
    with pytest.raises(ConfigError):
        load_secrets(_write(tmp_path, text, "s.yaml"))


def test_example_files_load():
    root = Path(__file__).parent.parent
    cfg = load_config(root / "assistant.example.yaml", {})
    assert cfg.model == "qwen3.5:27b"
    assert load_secrets(root / "secrets.example.yaml").cookie_secret


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_tool_steps", 0),
        ("max_tool_steps", 1),
        ("listen_port", 0),
        ("listen_port", 65536),
        ("model_timeout_s", 0),
        ("tool_timeout_s", -1),
        ("session_ttl_s", 0),
    ],
)
def test_numeric_bounds(tmp_path: Path, field: str, value: int):
    with pytest.raises(ConfigError, match=field):
        load_config(_write(tmp_path, _yaml(**{field: value})), {})


def test_numeric_bounds_edges_accepted(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _yaml(max_tool_steps=2, listen_port=65535)), {})
    assert (cfg.max_tool_steps, cfg.listen_port) == (2, 65535)
    assert load_config(_write(tmp_path, _yaml(listen_port=1)), {}).listen_port == 1


def test_gateway_public_url_optional_and_validated(tmp_path: Path) -> None:
    env = {
        "ASSISTANT_GATEWAY_URL": "http://gateway:8787",
        "ASSISTANT_OLLAMA_URL": "http://ollama.example:11434",
        "ASSISTANT_MODEL": "m",
        "ASSISTANT_CLIENT_SECRET_FILE": "/run/secret",
        "ASSISTANT_SECRETS_FILE": "/etc/pdl/s.yaml",
        "ASSISTANT_DB_PATH": str(tmp_path / "a.db"),
        "ASSISTANT_PUBLIC_URL": "https://assistant.example",
    }
    assert load_config(None, env).gateway_public_url is None
    cfg = load_config(None, {**env, "ASSISTANT_GATEWAY_PUBLIC_URL": "https://gateway.example"})
    assert cfg.gateway_public_url == "https://gateway.example"
    with pytest.raises(ConfigError, match="gateway_public_url"):
        load_config(None, {**env, "ASSISTANT_GATEWAY_PUBLIC_URL": "gateway.example"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ntfy_topic", "bad topic"),
        ("ntfy_topic", "a/b"),
        ("ntfy_topic", "x" * 65),
        ("briefing_retry_until", "25:00"),
        ("briefing_retry_until", "9:5"),
        ("briefing_timezone", "Mars/Olympus"),
        ("briefing_retry_interval_s", 0),
        ("briefing_news_count", 0),
    ],
)
def test_briefing_settings_validated(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(ConfigError, match=field):
        load_config(_write(tmp_path, _yaml(**{field: value})), {})


def test_briefing_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _yaml()), {})
    assert (cfg.briefing_timezone, cfg.briefing_retry_until) == ("America/Chicago", "10:00")
    assert (cfg.briefing_retry_interval_s, cfg.briefing_news_count) == (900, 3)
    assert cfg.ntfy_topic == "briefing"


def test_flagger_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _yaml()), {})
    assert cfg.flagger_config_file is None
    assert cfg.flagger_lookback_days == 45
    assert cfg.flagger_require_brain is False


def test_flagger_env_overrides(tmp_path: Path) -> None:
    env = {
        "ASSISTANT_FLAGGER_CONFIG_FILE": "/etc/flagger.yaml",
        "ASSISTANT_FLAGGER_LOOKBACK_DAYS": "90",
        "ASSISTANT_FLAGGER_REQUIRE_BRAIN": "Yes",
    }
    cfg = load_config(_write(tmp_path, _yaml()), env)
    assert cfg.flagger_config_file == "/etc/flagger.yaml"
    assert cfg.flagger_lookback_days == 90
    assert cfg.flagger_require_brain is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("TRUE", True), ("yes", True), ("0", False), ("False", False), ("NO", False)],
)
def test_flagger_require_brain_env_values(tmp_path: Path, raw: str, expected: bool) -> None:
    env = {"ASSISTANT_FLAGGER_REQUIRE_BRAIN": raw}
    assert load_config(_write(tmp_path, _yaml()), env).flagger_require_brain is expected


def test_flagger_require_brain_yaml_bool(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _yaml(flagger_require_brain=True)), {})
    assert cfg.flagger_require_brain is True


@pytest.mark.parametrize("raw", ["maybe", "", "2"])
def test_flagger_require_brain_rejects_other(tmp_path: Path, raw: str) -> None:
    env = {"ASSISTANT_FLAGGER_REQUIRE_BRAIN": raw}
    with pytest.raises(ConfigError, match="flagger_require_brain"):
        load_config(_write(tmp_path, _yaml()), env)


@pytest.mark.parametrize("days", [0, -1, 366])
def test_flagger_lookback_bounds(tmp_path: Path, days: int) -> None:
    with pytest.raises(ConfigError, match="flagger_lookback_days"):
        load_config(_write(tmp_path, _yaml(flagger_lookback_days=days)), {})


@pytest.mark.parametrize("days", [1, 365])
def test_flagger_lookback_bounds_accepted(tmp_path: Path, days: int) -> None:
    cfg = load_config(_write(tmp_path, _yaml(flagger_lookback_days=days)), {})
    assert cfg.flagger_lookback_days == days
