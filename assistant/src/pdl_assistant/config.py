"""Typed, validated assistant configuration and secrets loading."""

from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

import yaml

ENV_PREFIX = "ASSISTANT_"
_MIN_COOKIE_SECRET = 32
_URL_FIELDS = ("gateway_url", "ollama_url", "public_url")
_NOT_UTF8 = (
    "not UTF-8 text (PowerShell '>' writes UTF-16; "
    "use Set-Content -Encoding utf8 or run the command in bash)"
)


class ConfigError(ValueError):
    """Raised when the configuration or secrets file is invalid."""


@dataclass(frozen=True)
class AssistantConfig:
    gateway_url: str
    ollama_url: str
    model: str
    client_secret_file: str
    secrets_file: str
    db_path: str
    public_url: str
    client_id: str = "personal-assistant"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8790
    max_tool_steps: int = 6
    model_timeout_s: float = 120
    tool_timeout_s: float = 60
    session_ttl_s: int = 43200
    system_prompt_extra: str = ""


@dataclass(frozen=True)
class AssistantSecrets:
    api_token_hashes: Mapping[str, str]
    cookie_secret: str


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: {_NOT_UTF8}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return raw


def _cast(name: str, kind: type, value: Any) -> Any:
    """Cast ``value`` to the field type; a bad value is a ConfigError naming the field."""
    try:
        if kind is str:
            if isinstance(value, (dict, list)):
                raise TypeError("expected a scalar")
            return str(value)
        if isinstance(value, bool):
            raise TypeError("expected a number")
        return kind(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: invalid value {value!r} ({exc})") from exc


def _check_url(name: str, value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{name} must be an http(s) URL, got {value!r}")


def load_config(path: Path | None, env: Mapping[str, str]) -> AssistantConfig:
    """Load config from an optional YAML file, overridden by ``ASSISTANT_<FIELD>`` env vars."""
    raw = _read_mapping(path) if path is not None else {}
    specs = {f.name: f for f in fields(AssistantConfig)}
    unknown = sorted(set(raw) - set(specs))
    if unknown:
        raise ConfigError(f"unknown key(s) in config: {', '.join(unknown)}")
    merged = dict(raw)
    for name in specs:
        env_value = env.get(f"{ENV_PREFIX}{name.upper()}")
        if env_value is not None:
            merged[name] = env_value
    missing = sorted(n for n, f in specs.items() if f.default is MISSING and n not in merged)
    if missing:
        raise ConfigError(f"missing required config key(s): {', '.join(missing)}")
    values = {n: _cast(n, specs[n].type, v) for n, v in merged.items()}  # type: ignore[arg-type]
    for name in _URL_FIELDS:
        _check_url(name, values[name])
    return AssistantConfig(**values)


def load_secrets(path: Path) -> AssistantSecrets:
    """Load the assistant secrets file (API token hashes and the cookie signing secret)."""
    raw = _read_mapping(path)
    unknown = sorted(set(raw) - {"api_tokens", "cookie_secret"})
    if unknown:
        raise ConfigError(f"unknown key(s) in secrets: {', '.join(unknown)}")
    tokens = raw.get("api_tokens") or {}
    if not isinstance(tokens, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in tokens.items()
    ):
        raise ConfigError("api_tokens must map names to hash strings")
    cookie = raw.get("cookie_secret")
    if not isinstance(cookie, str) or len(cookie) < _MIN_COOKIE_SECRET:
        raise ConfigError(f"cookie_secret is required and must be >= {_MIN_COOKIE_SECRET} chars")
    return AssistantSecrets(MappingProxyType(dict(tokens)), cookie)
