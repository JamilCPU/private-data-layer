"""Live tokens follow the current grant: config changes narrow or kill issued tokens."""

import dataclasses

import httpx
from mcp.shared.auth import OAuthClientInformationFull

from pdl_gateway.config import GatewayConfig, GatewaySecrets, GrantConfig
from pdl_gateway.oauth import GatewayAuthProvider
from pdl_gateway.store import TokenStore

from .conftest import PA_SECRET, FakeClock
from .oauth_helpers import ASSISTANT_SCOPES, session


def _narrowed(config: GatewayConfig, grant_name: str, drop: str) -> GatewayConfig:
    grants = tuple(
        dataclasses.replace(g, scopes=tuple(s for s in g.scopes if s != drop))
        if g.name == grant_name
        else g
        for g in config.grants
    )
    return dataclasses.replace(config, grants=grants)


def _without(config: GatewayConfig, grant_name: str) -> GatewayConfig:
    return dataclasses.replace(
        config, grants=tuple(g for g in config.grants if g.name != grant_name)
    )


def _rebuilt(
    config: GatewayConfig, secrets: GatewaySecrets, store: TokenStore, clock: FakeClock
) -> GatewayAuthProvider:
    return GatewayAuthProvider(config, secrets, store, clock=clock)


async def test_headless_token_loses_scopes_removed_from_its_grant(
    provider: GatewayAuthProvider,
    config: GatewayConfig,
    secrets: GatewaySecrets,
    store: TokenStore,
    clock: FakeClock,
) -> None:
    token = provider.issue_client_credentials("personal-assistant", PA_SECRET).access_token
    narrowed = _narrowed(config, "personal-assistant", "automations:read")
    loaded = await _rebuilt(narrowed, secrets, store, clock).load_access_token(token)
    assert loaded is not None
    assert loaded.scopes == [s for s in ASSISTANT_SCOPES if s != "automations:read"]


async def test_headless_token_dies_with_its_grant(
    provider: GatewayAuthProvider,
    config: GatewayConfig,
    secrets: GatewaySecrets,
    store: TokenStore,
    clock: FakeClock,
) -> None:
    token = provider.issue_client_credentials("personal-assistant", PA_SECRET).access_token
    gone = _without(config, "personal-assistant")
    assert await _rebuilt(gone, secrets, store, clock).load_access_token(token) is None


async def test_headless_token_dies_when_its_grant_becomes_interactive(
    provider: GatewayAuthProvider,
    config: GatewayConfig,
    secrets: GatewaySecrets,
    store: TokenStore,
    clock: FakeClock,
) -> None:
    token = provider.issue_client_credentials("personal-assistant", PA_SECRET).access_token
    grants = tuple(
        GrantConfig(g.name, "interactive", g.scopes) if g.name == "personal-assistant" else g
        for g in config.grants
    )
    changed = dataclasses.replace(config, grants=grants)
    assert await _rebuilt(changed, secrets, store, clock).load_access_token(token) is None


async def test_dcr_tokens_follow_the_interactive_grant(
    http: httpx.AsyncClient,
    config: GatewayConfig,
    secrets: GatewaySecrets,
    store: TokenStore,
    clock: FakeClock,
) -> None:
    client_json, tokens = await session(http)
    client = OAuthClientInformationFull.model_validate(client_json)
    narrowed = _narrowed(config, config.interactive_grant, "automations:read")
    rebuilt = _rebuilt(narrowed, secrets, store, clock)

    access = await rebuilt.load_access_token(tokens["access_token"])
    assert access is not None
    assert access.scopes == ["homestudio:read"]
    refresh = await rebuilt.load_refresh_token(client, tokens["refresh_token"])
    assert refresh is not None
    assert refresh.scopes == ["homestudio:read"]
    rotated = await rebuilt.exchange_refresh_token(client, refresh, [])
    assert rotated.scope == "homestudio:read"


async def test_token_of_a_vanished_dcr_client_is_dead(
    provider: GatewayAuthProvider, store: TokenStore, clock: FakeClock
) -> None:
    expires = int(clock()) + 60
    store.save_token("access", "orphan", "no-such-client", ["homestudio:read"], expires)
    store.save_token("refresh", "orphan-r", "no-such-client", ["homestudio:read"], expires)
    ghost = OAuthClientInformationFull(client_id="no-such-client", redirect_uris=None)
    assert await provider.load_access_token("orphan") is None
    assert await provider.load_refresh_token(ghost, "orphan-r") is None
