"""Typed, validated gateway configuration and secrets loading."""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

import yaml

DEFAULT_SECRET_HEADER = "X-Gateway-Secret"
_GRANT_KINDS = ("headless", "interactive")
_SCOPE_VERBS = ("read", "audit")  # v1 has no write scopes
_NOT_UTF8 = (
    "not UTF-8 text (PowerShell '>' writes UTF-16; "
    "use Set-Content -Encoding utf8 or run the command in bash)"
)


class ConfigError(ValueError):
    """Raised when the configuration or secrets file is invalid."""


@dataclass(frozen=True)
class BackendConfig:
    name: str
    url: str
    scope: str
    allow_tools: tuple[str, ...] | None = None
    secret_header: str | None = None
    secret_env: str | None = None
    bearer_env: str | None = None
    ca_file: str | None = None


@dataclass(frozen=True)
class GrantConfig:
    name: str
    kind: Literal["headless", "interactive"]
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class GatewayConfig:
    issuer_url: str
    resource_url: str
    listen_host: str
    listen_port: int
    scopes: tuple[str, ...]
    backends: tuple[BackendConfig, ...]
    grants: tuple[GrantConfig, ...]
    db_path: str
    audit_dir: str
    interactive_grant: str = "interactive-default"
    access_token_ttl_s: int = 3600
    refresh_token_ttl_s: int = 43200
    auth_code_ttl_s: int = 300
    tools_cache_ttl_s: int = 60

    def grant(self, name: str) -> GrantConfig | None:
        """Return the grant called ``name``, or None."""
        return next((g for g in self.grants if g.name == name), None)

    @property
    def interactive_scopes(self) -> tuple[str, ...]:
        """Scopes offered to interactive (human-approved) clients."""
        grant = self.grant(self.interactive_grant)
        return grant.scopes if grant is not None else ()


@dataclass(frozen=True)
class GatewaySecrets:
    approval_passphrase_hash: str
    client_secret_hashes: Mapping[str, str]


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


def _check_keys(raw: dict[str, Any], allowed: set[str], section: str) -> None:
    """Reject keys we do not know: a typo must never silently weaken a policy."""
    for key in sorted(set(raw) - allowed):
        raise ConfigError(f"unknown key {key!r} in {section}")


def _string_list(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{what} must be a list of strings")
    return tuple(value)


def _env_name(raw: dict[str, Any], key: str, backend: str) -> str | None:
    if key not in raw:
        return None
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"backend {backend!r}: {key} must be a non-empty string")
    return value


def _backend(raw: dict[str, Any]) -> BackendConfig:
    name = raw["name"]
    _check_keys(raw, {f.name for f in fields(BackendConfig)}, f"backend {name!r}")
    secret_env = _env_name(raw, "secret_env", name)
    bearer_env = _env_name(raw, "bearer_env", name)
    if (secret_env is None) == (bearer_env is None):
        raise ConfigError(f"backend {name!r} must set exactly one of secret_env or bearer_env")
    header = raw.get("secret_header", DEFAULT_SECRET_HEADER) if secret_env else None
    allow_tools = None
    if "allow_tools" in raw:
        allow_tools = _string_list(raw["allow_tools"], f"backend {name!r}: allow_tools")
    return BackendConfig(
        name=name,
        url=raw["url"],
        scope=raw["scope"],
        allow_tools=allow_tools,
        secret_header=header,
        secret_env=secret_env,
        bearer_env=bearer_env,
        ca_file=raw.get("ca_file"),
    )


def _grant(raw: dict[str, Any]) -> GrantConfig:
    name = raw["name"]
    _check_keys(raw, {f.name for f in fields(GrantConfig)}, f"grant {name!r}")
    kind = raw["kind"]
    if kind not in _GRANT_KINDS:
        raise ConfigError(f"grant {name!r} has unknown kind {kind!r}")
    return GrantConfig(
        name=name,
        kind=cast(Literal["headless", "interactive"], kind),
        scopes=_string_list(raw["scopes"], f"grant {name!r}: scopes"),
    )


def _check_scope(scope: str) -> None:
    _, sep, verb = scope.rpartition(":")
    if not sep or verb not in _SCOPE_VERBS:
        allowed = " or ".join(f"<name>:{v}" for v in _SCOPE_VERBS)
        raise ConfigError(f"scope {scope!r} is not allowed: v1 scopes are {allowed}")


def _validate(cfg: GatewayConfig) -> None:
    for scope in cfg.scopes:
        _check_scope(scope)
    declared = set(cfg.scopes)
    names = [b.name for b in cfg.backends]
    for name in sorted({n for n in names if names.count(n) > 1}):
        raise ConfigError(f"duplicate backend name {name!r}")
    grant_names = [g.name for g in cfg.grants]
    for name in sorted({n for n in grant_names if grant_names.count(n) > 1}):
        raise ConfigError(f"duplicate grant name {name!r}")
    for backend in cfg.backends:
        if backend.scope not in declared:
            raise ConfigError(f"backend {backend.name!r} uses undeclared scope {backend.scope!r}")
    for grant in cfg.grants:
        for scope in grant.scopes:
            if scope not in declared:
                raise ConfigError(f"grant {grant.name!r} uses undeclared scope {scope!r}")
    interactive = cfg.grant(cfg.interactive_grant)
    if interactive is None:
        raise ConfigError(f"interactive grant {cfg.interactive_grant!r} is not defined")
    if interactive.kind != "interactive":
        raise ConfigError(f"grant {cfg.interactive_grant!r} must have kind 'interactive'")


def _build_config(raw: dict[str, Any]) -> GatewayConfig:
    _check_keys(raw, {f.name for f in fields(GatewayConfig)}, "top level")
    optional = {
        key: int(raw[key])
        for key in (
            "access_token_ttl_s",
            "refresh_token_ttl_s",
            "auth_code_ttl_s",
            "tools_cache_ttl_s",
        )
        if key in raw
    }
    if "interactive_grant" in raw:
        optional["interactive_grant"] = raw["interactive_grant"]
    return GatewayConfig(
        issuer_url=raw["issuer_url"],
        resource_url=raw["resource_url"],
        listen_host=raw["listen_host"],
        listen_port=int(raw["listen_port"]),
        scopes=tuple(raw["scopes"]),
        backends=tuple(_backend(b) for b in raw["backends"]),
        grants=tuple(_grant(g) for g in raw["grants"]),
        db_path=raw["db_path"],
        audit_dir=raw["audit_dir"],
        **optional,
    )


def load_config(path: Path) -> GatewayConfig:
    """Load and validate the gateway configuration file."""
    raw = _read_mapping(path)
    try:
        cfg = _build_config(raw)
    except KeyError as exc:
        raise ConfigError(f"{path}: missing required key {exc.args[0]!r}") from exc
    except ConfigError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: invalid value: {exc}") from exc
    _validate(cfg)
    return cfg


def _hash_text(value: Any, what: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {what} must be a non-empty string")
    return value


def load_secrets(path: Path) -> GatewaySecrets:
    """Load the hashed approval passphrase and client secrets."""
    raw = _read_mapping(path)
    for key in ("approval_passphrase", "client_secrets"):
        if key not in raw:
            raise ConfigError(f"{path}: missing required key {key!r}")
    clients = raw["client_secrets"] or {}
    if not isinstance(clients, dict):
        raise ConfigError(f"{path}: client_secrets must be a mapping")
    hashes = {
        str(client): _hash_text(value, f"client_secrets[{client!r}]", path)
        for client, value in clients.items()
    }
    return GatewaySecrets(
        approval_passphrase_hash=_hash_text(
            raw["approval_passphrase"], "approval_passphrase", path
        ),
        client_secret_hashes=MappingProxyType(hashes),
    )


def check_secrets(config: GatewayConfig, secrets: GatewaySecrets) -> None:
    """Raise ConfigError naming every headless grant without a client secret hash."""
    missing = [
        g.name
        for g in config.grants
        if g.kind == "headless" and g.name not in secrets.client_secret_hashes
    ]
    if missing:
        raise ConfigError(f"no client secret hash for headless grant(s): {', '.join(missing)}")
