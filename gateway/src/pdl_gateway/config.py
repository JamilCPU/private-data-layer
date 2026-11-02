"""Typed, validated gateway configuration and secrets loading."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

import yaml

DEFAULT_SECRET_HEADER = "X-Gateway-Secret"
_GRANT_KINDS = ("headless", "interactive")


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
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return raw


def _optional_tuple(value: Any) -> tuple[str, ...] | None:
    return None if value is None else tuple(str(v) for v in value)


def _backend(raw: dict[str, Any]) -> BackendConfig:
    name = raw["name"]
    has_secret, has_bearer = "secret_env" in raw, "bearer_env" in raw
    if has_secret == has_bearer:
        raise ConfigError(f"backend {name!r} must set exactly one of secret_env or bearer_env")
    header = raw.get("secret_header", DEFAULT_SECRET_HEADER) if has_secret else None
    return BackendConfig(
        name=name,
        url=raw["url"],
        scope=raw["scope"],
        allow_tools=_optional_tuple(raw.get("allow_tools")),
        secret_header=header,
        secret_env=raw.get("secret_env"),
        bearer_env=raw.get("bearer_env"),
        ca_file=raw.get("ca_file"),
    )


def _grant(raw: dict[str, Any]) -> GrantConfig:
    kind = raw["kind"]
    if kind not in _GRANT_KINDS:
        raise ConfigError(f"grant {raw['name']!r} has unknown kind {kind!r}")
    return GrantConfig(
        name=raw["name"],
        kind=cast(Literal["headless", "interactive"], kind),
        scopes=tuple(raw["scopes"]),
    )


def _validate(cfg: GatewayConfig) -> None:
    declared = set(cfg.scopes)
    names = [b.name for b in cfg.backends]
    for name in sorted({n for n in names if names.count(n) > 1}):
        raise ConfigError(f"duplicate backend name {name!r}")
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


def load_secrets(path: Path) -> GatewaySecrets:
    """Load the hashed approval passphrase and client secrets."""
    raw = _read_mapping(path)
    for key in ("approval_passphrase", "client_secrets"):
        if key not in raw:
            raise ConfigError(f"{path}: missing required key {key!r}")
    clients = raw["client_secrets"] or {}
    if not isinstance(clients, dict):
        raise ConfigError(f"{path}: client_secrets must be a mapping")
    return GatewaySecrets(
        approval_passphrase_hash=str(raw["approval_passphrase"]),
        client_secret_hashes=MappingProxyType({str(k): str(v) for k, v in clients.items()}),
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
