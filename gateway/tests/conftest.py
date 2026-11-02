"""Shared fixtures: config, secrets, store, fake clock, OAuth provider and auth app."""

import dataclasses
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette

from pdl_gateway.auth_app import build_auth_routes
from pdl_gateway.config import GatewayConfig, GatewaySecrets, load_config
from pdl_gateway.oauth import GatewayAuthProvider
from pdl_gateway.secrets import hash_secret
from pdl_gateway.store import TokenStore

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "gateway.example.yaml"
ISSUER = "http://localhost:8787"
PASSPHRASE = "open sesame"
PA_SECRET = "pa-secret"
_FAST_ITERATIONS = 1000


class FakeClock:
    """A settable clock; starts at real time because the SDK checks some expiries itself."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def config(tmp_path: Path) -> GatewayConfig:
    return dataclasses.replace(
        load_config(EXAMPLE_CONFIG),
        issuer_url=ISSUER,
        resource_url=f"{ISSUER}/mcp",
        db_path=str(tmp_path / "gateway.db"),
        audit_dir=str(tmp_path / "audit"),
    )


@pytest.fixture
def secrets() -> GatewaySecrets:
    return GatewaySecrets(
        approval_passphrase_hash=hash_secret(PASSPHRASE, iterations=_FAST_ITERATIONS),
        client_secret_hashes={
            "personal-assistant": hash_secret(PA_SECRET, iterations=_FAST_ITERATIONS)
        },
    )


@pytest.fixture
def store(config: GatewayConfig) -> Iterator[TokenStore]:
    s = TokenStore(config.db_path)
    yield s
    s.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(time.time())


@pytest.fixture
def provider(
    config: GatewayConfig, secrets: GatewaySecrets, store: TokenStore, clock: FakeClock
) -> GatewayAuthProvider:
    return GatewayAuthProvider(config, secrets, store, clock=clock)


@pytest.fixture
def auth_app(config: GatewayConfig, provider: GatewayAuthProvider) -> Starlette:
    return Starlette(routes=build_auth_routes(config, provider))


@pytest.fixture
async def http(auth_app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app),
        base_url=ISSUER,
        follow_redirects=False,
    ) as client:
        yield client
