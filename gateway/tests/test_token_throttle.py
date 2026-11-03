"""``/token`` client_credentials lockout: the same semantics as ``/approve``."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.applications import Starlette

from pdl_gateway.auth_app import build_auth_routes
from pdl_gateway.config import GatewayConfig
from pdl_gateway.oauth import GatewayAuthProvider
from pdl_gateway.throttle import AttemptThrottle

from .conftest import ISSUER, PA_SECRET, FakeClock
from .oauth_helpers import client_credentials, session


@pytest.fixture
def token_clock() -> FakeClock:
    return FakeClock(1000.0)


@pytest.fixture
async def guarded(
    config: GatewayConfig, provider: GatewayAuthProvider, token_clock: FakeClock
) -> AsyncIterator[httpx.AsyncClient]:
    throttle = AttemptThrottle(limit=5, window_s=600, clock=token_clock, what="token")
    app = Starlette(routes=build_auth_routes(config, provider, token_throttle=throttle))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        yield client


async def _fail_five_times(http: httpx.AsyncClient) -> None:
    for _ in range(5):
        resp = await client_credentials(http, "personal-assistant", "wrong")
        assert resp.status_code == 401


async def test_five_wrong_secrets_lock_client_credentials(guarded: httpx.AsyncClient) -> None:
    await _fail_five_times(guarded)
    resp = await client_credentials(guarded, "personal-assistant", PA_SECRET)
    assert resp.status_code == 429
    assert resp.json()["error"] == "invalid_client"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["retry-after"] == "600"


async def test_token_lock_lifts_after_window(
    guarded: httpx.AsyncClient, token_clock: FakeClock
) -> None:
    await _fail_five_times(guarded)
    token_clock.advance(599)
    assert (await client_credentials(guarded, "personal-assistant", PA_SECRET)).status_code == 429
    token_clock.advance(2)
    resp = await client_credentials(guarded, "personal-assistant", PA_SECRET)
    assert resp.status_code == 200, resp.text


async def test_concurrent_wrong_secrets_cannot_overrun_the_lock(
    guarded: httpx.AsyncClient,
) -> None:
    guesses = [client_credentials(guarded, "personal-assistant", f"g{i}") for i in range(8)]
    statuses = sorted(resp.status_code for resp in await asyncio.gather(*guesses))
    assert statuses == [401] * 5 + [429] * 3


async def test_token_lock_leaves_other_grants_alone(guarded: httpx.AsyncClient) -> None:
    await _fail_five_times(guarded)
    _, tokens = await session(guarded)  # authorization_code still works
    assert tokens["access_token"]


async def test_default_routes_have_a_token_lock_separate_from_approvals(
    http: httpx.AsyncClient,
) -> None:
    await _fail_five_times(http)
    assert (await client_credentials(http, "personal-assistant", PA_SECRET)).status_code == 429
    _, tokens = await session(http)  # approvals are not locked by /token failures
    assert tokens["access_token"]
