"""Negative paths: scope narrowing, cross-client codes, grant confusion, cheap rejection."""

import httpx
import pytest
from mcp.server.auth.provider import TokenError

from pdl_gateway import oauth
from pdl_gateway.oauth import GatewayAuthProvider

from .oauth_helpers import (
    INTERACTIVE,
    INVALID_CLIENT,
    REDIRECT,
    VERIFIER,
    approve_flow,
    client_credentials,
    dcr,
    exchange,
    pkce,
    refresh,
    session,
)


async def test_refresh_with_narrower_scope_keeps_grant_scopes(
    http: httpx.AsyncClient, provider: GatewayAuthProvider
) -> None:
    client, first = await session(http)
    resp = await refresh(http, client, first["refresh_token"], scope="homestudio:read")
    assert resp.status_code == 200, resp.text
    second = resp.json()
    assert second["scope"] == INTERACTIVE
    access = await provider.load_access_token(second["access_token"])
    assert access is not None
    assert access.scopes == INTERACTIVE.split()
    again = await refresh(http, client, second["refresh_token"])
    assert again.status_code == 200, again.text
    assert again.json()["scope"] == INTERACTIVE


async def test_dcr_client_cannot_use_client_credentials(http: httpx.AsyncClient) -> None:
    client = await dcr(http, token_endpoint_auth_method="client_secret_post")
    assert client["client_secret"]
    resp = await client_credentials(http, client["client_id"], client["client_secret"])
    assert resp.status_code == 401
    assert resp.json() == INVALID_CLIENT


async def test_wrong_code_verifier_is_invalid_grant(http: httpx.AsyncClient) -> None:
    client = await dcr(http)
    code = await approve_flow(http, client)
    other_verifier, _ = pkce()
    resp = await exchange(http, client, code, verifier=other_verifier)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_code_cannot_be_redeemed_by_another_client(http: httpx.AsyncClient) -> None:
    client_a = await dcr(http)
    client_b = await dcr(http, client_name="Other")
    code = await approve_flow(http, client_a)
    stolen = await exchange(http, client_b, code)
    assert stolen.status_code == 400
    assert stolen.json()["error"] == "invalid_grant"
    assert (await exchange(http, client_a, code)).status_code == 200


async def test_headless_client_cannot_use_refresh_grant(http: httpx.AsyncClient) -> None:
    _, tokens = await session(http)
    headless = {"client_id": "personal-assistant"}
    resp = await refresh(http, headless, tokens["refresh_token"])
    assert resp.status_code == 400
    assert resp.json()["error"] in ("unsupported_grant_type", "invalid_grant")


async def test_headless_client_cannot_use_code_grant(http: httpx.AsyncClient) -> None:
    resp = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "anything",
            "redirect_uri": REDIRECT,
            "client_id": "personal-assistant",
            "code_verifier": VERIFIER,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] in ("unsupported_grant_type", "invalid_grant")


def test_unknown_approval_request_is_rejected_before_pbkdf2(
    provider: GatewayAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_: object) -> bool:
        raise AssertionError("verify_secret must not run for an unknown request")

    monkeypatch.setattr(oauth, "verify_secret", fail)
    with pytest.raises(LookupError):
        provider.complete_approval("no-such-request", "open sesame", True)


async def test_code_exchange_is_refused_once_the_code_is_gone(
    http: httpx.AsyncClient, provider: GatewayAuthProvider
) -> None:
    client = await dcr(http)
    code_text = await approve_flow(http, client)
    info = await provider.get_client(client["client_id"])
    assert info is not None
    code = await provider.load_authorization_code(info, code_text)
    assert code is not None
    await provider.exchange_authorization_code(info, code)
    with pytest.raises(TokenError) as exc:
        await provider.exchange_authorization_code(info, code)
    assert exc.value.error == "invalid_grant"


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:6274/callback",
        "http://127.0.0.1:33418/",
        "http://[::1]:8080/oauth/cb",
        "http://localhost/callback",
    ],
)
async def test_dcr_accepts_loopback_redirect_uris(http: httpx.AsyncClient, uri: str) -> None:
    client = await dcr(http, redirect_uris=[uri])
    assert client["redirect_uris"] == [uri]


@pytest.mark.parametrize(
    "uris",
    [
        ["https://evil.example/callback"],
        ["http://localhost.evil.example/callback"],
        ["http://localhost:6274/callback", "http://203.0.113.5:6274/callback"],
        ["myapp://callback"],
    ],
)
async def test_dcr_refuses_non_loopback_redirect_uris(
    http: httpx.AsyncClient, uris: list[str]
) -> None:
    body = {
        "redirect_uris": uris,
        "client_name": "Claude Code",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    resp = await http.post("/register", json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    ("size", "status"),
    [(2 * 1024 * 1024, 400), (5 * 1024 * 1024, 413)],
    ids=["2MB-field-limit", "5MB-sdk-body-limit"],
)
async def test_approve_post_refuses_oversized_body(
    http: httpx.AsyncClient, size: int, status: int
) -> None:
    """Starlette caps one form field at 1 MB; the SDK's limit (4 MB) caps the whole body."""
    form = {"request": "x", "passphrase": "p" * size, "decision": "approve"}
    resp = await http.post("/approve", data=form)
    assert resp.status_code == status
