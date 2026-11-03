"""Tests for the human approval page."""

import asyncio
import html
from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.applications import Starlette

from pdl_gateway.auth_app import build_auth_routes
from pdl_gateway.config import GatewayConfig
from pdl_gateway.oauth import GatewayAuthProvider
from pdl_gateway.throttle import AttemptThrottle

from .conftest import ISSUER, FakeClock
from .oauth_helpers import REDIRECT, begin, dcr, decide

UNKNOWN = "This approval request is unknown or has expired. Start again from your client."


async def test_get_approve_unknown_request_is_400(http: httpx.AsyncClient) -> None:
    resp = await http.get("/approve", params={"request": "nope"})
    assert resp.status_code == 400
    assert UNKNOWN in resp.text
    missing = await http.get("/approve")
    assert missing.status_code == 400


async def test_approve_page_lists_scopes_and_client(http: httpx.AsyncClient) -> None:
    client = await dcr(http)
    request_id = await begin(http, client)
    resp = await http.get("/approve", params={"request": request_id})
    assert resp.status_code == 200
    page = resp.text
    assert "<title>Approve access</title>" in page
    assert "Claude Code" in page
    for scope in ("homestudio:read", "automations:read"):
        assert scope in page
    assert "bank:read" not in page
    assert 'type="password"' in page
    assert 'name="passphrase"' in page
    assert 'value="approve"' in page
    assert 'value="deny"' in page
    assert "http://localhost:6274/callback" in page
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


async def test_approve_page_escapes_client_name(http: httpx.AsyncClient) -> None:
    evil = "<script>alert(1)</script>"
    client = await dcr(http, client_name=evil)
    resp = await http.get(
        "/authorize",
        params={
            "client_id": client["client_id"],
            "response_type": "code",
            "code_challenge": "x" * 43,
            "state": "s",
        },
    )
    request_id = resp.headers["location"].split("request=")[1]
    page = (await http.get("/approve", params={"request": request_id})).text
    assert evil not in page
    assert html.escape(evil) in page


async def test_post_approve_missing_fields_is_400(http: httpx.AsyncClient) -> None:
    assert (await http.post("/approve", data={})).status_code == 400
    partial = {"request": "r", "passphrase": "open sesame"}
    assert (await http.post("/approve", data=partial)).status_code == 400
    bogus = {**partial, "decision": "maybe"}
    assert (await http.post("/approve", data=bogus)).status_code == 400


THROTTLED = "Too many attempts; try again in 10 minutes"


@pytest.fixture
def throttle_clock() -> FakeClock:
    return FakeClock(1000.0)


@pytest.fixture
async def throttled(
    config: GatewayConfig, provider: GatewayAuthProvider, throttle_clock: FakeClock
) -> AsyncIterator[httpx.AsyncClient]:
    throttle = AttemptThrottle(limit=5, window_s=600, clock=throttle_clock)
    app = Starlette(routes=build_auth_routes(config, provider, throttle=throttle))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        yield client


async def test_five_wrong_passphrases_lock_approvals_across_requests(
    throttled: httpx.AsyncClient,
) -> None:
    client = await dcr(throttled)
    first = await begin(throttled, client, state="a")
    second = await begin(throttled, client, state="b")
    for request_id in (first, first, first, second, second):
        resp = await decide(throttled, request_id, "approve", passphrase="wrong")
        assert resp.status_code == 200
        assert "Wrong passphrase" in resp.text

    fresh = await begin(throttled, client, state="c")  # a new request id does not help
    resp = await decide(throttled, fresh, "approve")
    assert resp.status_code == 429
    assert THROTTLED in resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert (await decide(throttled, first, "deny")).status_code == 429


async def test_throttle_lifts_after_window(
    throttled: httpx.AsyncClient, throttle_clock: FakeClock
) -> None:
    client = await dcr(throttled)
    request_id = await begin(throttled, client)
    for _ in range(5):
        await decide(throttled, request_id, "approve", passphrase="wrong")
    throttle_clock.advance(599)
    assert (await decide(throttled, request_id, "approve")).status_code == 429

    throttle_clock.advance(2)
    resp = await decide(throttled, request_id, "approve")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(REDIRECT)


async def test_concurrent_guesses_cannot_overrun_the_throttle(throttled: httpx.AsyncClient) -> None:
    client = await dcr(throttled)
    request_id = await begin(throttled, client)
    guesses = [decide(throttled, request_id, "approve", passphrase=f"g{i}") for i in range(8)]
    statuses = sorted(resp.status_code for resp in await asyncio.gather(*guesses))
    assert statuses == [200] * 5 + [429] * 3


def test_throttle_counts_only_failures_inside_the_window() -> None:
    clock = FakeClock(0.0)
    throttle = AttemptThrottle(limit=3, window_s=100, clock=clock)
    throttle.record_failure()
    throttle.record_failure()
    clock.advance(150)  # both earlier failures have aged out
    throttle.record_failure()
    throttle.record_failure()
    assert not throttle.blocked()
    throttle.record_failure()
    assert throttle.blocked()
    clock.advance(100)
    assert not throttle.blocked()
