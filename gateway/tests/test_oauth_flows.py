"""End-to-end OAuth flows against the auth routes: DCR, PKCE approval, refresh, client credentials."""

import base64
import hashlib
from secrets import token_urlsafe
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from pydantic import AnyUrl

from pdl_gateway.oauth import GatewayAuthProvider

from .conftest import PA_SECRET, PASSPHRASE, FakeClock

REDIRECT = "http://localhost:6274/callback"
INTERACTIVE = "homestudio:read automations:read"
ASSISTANT_SCOPES = [
    "homestudio:read",
    "automations:read",
    "email:read",
    "sms:read",
    "bank:read",
    "search:read",
    "gateway:audit",
]
INVALID_CLIENT = {"error": "invalid_client", "error_description": "unknown client or wrong secret"}


def pkce() -> tuple[str, str]:
    verifier = token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


VERIFIER, CHALLENGE = pkce()


async def dcr(http: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "redirect_uris": [REDIRECT],
        "client_name": "Claude Code",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        **overrides,
    }
    resp = await http.post("/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def authorize(
    http: httpx.AsyncClient, client_id: str, state: str = "st-1", **extra: str
) -> httpx.Response:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": state,
        **extra,
    }
    return await http.get("/authorize", params=params)


async def begin(
    http: httpx.AsyncClient, client: dict[str, Any], state: str = "st-1", **extra: str
) -> str:
    """Run /authorize and GET /approve; return the approval request id."""
    resp = await authorize(http, client["client_id"], state, **extra)
    assert resp.status_code == 302, resp.text
    location = urlparse(resp.headers["location"])
    assert location.path == "/approve"
    request_id = parse_qs(location.query)["request"][0]
    page = await http.get("/approve", params={"request": request_id})
    assert page.status_code == 200
    assert "Claude Code" in page.text
    assert "homestudio:read" in page.text
    return request_id


async def decide(
    http: httpx.AsyncClient, request_id: str, decision: str, passphrase: str = PASSPHRASE
) -> httpx.Response:
    form = {"request": request_id, "passphrase": passphrase, "decision": decision}
    return await http.post("/approve", data=form)


def query(location: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(location).query)


async def approve_flow(
    http: httpx.AsyncClient, client: dict[str, Any], passphrase: str = PASSPHRASE, **extra: str
) -> str:
    """Complete an approval; return the authorization code."""
    request_id = await begin(http, client, **extra)
    resp = await decide(http, request_id, "approve", passphrase)
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    assert location.startswith(REDIRECT)
    assert query(location)["state"] == [extra.get("state", "st-1")]
    return query(location)["code"][0]


async def exchange(http: httpx.AsyncClient, client: dict[str, Any], code: str) -> httpx.Response:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client["client_id"],
        "code_verifier": VERIFIER,
    }
    return await http.post("/token", data=form)


async def refresh(http: httpx.AsyncClient, client: dict[str, Any], token: str) -> httpx.Response:
    form = {"grant_type": "refresh_token", "refresh_token": token, "client_id": client["client_id"]}
    return await http.post("/token", data=form)


async def session(http: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """Register, approve and exchange; return (client, token response)."""
    client = await dcr(http)
    resp = await exchange(http, client, await approve_flow(http, client))
    assert resp.status_code == 200, resp.text
    return client, resp.json()


async def client_credentials(
    http: httpx.AsyncClient, client_id: str, client_secret: str
) -> httpx.Response:
    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return await http.post("/token", data=form)


async def test_metadata_advertises_endpoints(http: httpx.AsyncClient) -> None:
    meta = (await http.get("/.well-known/oauth-authorization-server")).json()
    assert meta["registration_endpoint"] == "http://localhost:8787/register"
    assert meta["revocation_endpoint"] == "http://localhost:8787/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert "client_credentials" in meta["grant_types_supported"]
    assert meta["grant_types_supported"] == [
        "authorization_code",
        "refresh_token",
        "client_credentials",
    ]
    prm = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm["authorization_servers"] in (["http://localhost:8787/"], ["http://localhost:8787"])
    assert prm["resource"] == "http://localhost:8787/mcp"


async def test_dcr_then_code_flow_issues_interactive_scopes(
    http: httpx.AsyncClient, provider: GatewayAuthProvider
) -> None:
    client = await dcr(http)
    code = await approve_flow(http, client, scope="homestudio:read")  # narrower ask, same grant
    resp = await exchange(http, client, code)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == INTERACTIVE
    assert body["refresh_token"]
    assert body["expires_in"] == 3600
    assert body["token_type"] == "Bearer"
    assert resp.headers["cache-control"] == "no-store"
    access = await provider.load_access_token(body["access_token"])
    assert access is not None
    assert access.claims == {"client_name": "Claude Code"}
    assert access.client_id == client["client_id"]
    assert access.resource == "http://localhost:8787/mcp"
    assert access.scopes == INTERACTIVE.split()


async def test_state_round_trips(http: httpx.AsyncClient) -> None:
    client = await dcr(http)
    state = "opaque state/with?chars&=="
    approved = await decide(http, await begin(http, client, state=state), "approve")
    assert query(approved.headers["location"])["state"] == [state]
    denied = await decide(http, await begin(http, client, state=state), "deny")
    assert query(denied.headers["location"])["state"] == [state]


async def test_wrong_passphrase_rerenders_form(http: httpx.AsyncClient) -> None:
    client = await dcr(http)
    request_id = await begin(http, client)
    resp = await decide(http, request_id, "approve", passphrase="guess")
    assert resp.status_code == 200
    assert "Wrong passphrase" in resp.text
    assert "location" not in resp.headers
    retry = await decide(http, request_id, "approve")  # the request survives a wrong guess
    assert retry.status_code == 303
    assert "code" in query(retry.headers["location"])


async def test_deny_redirects_with_access_denied(http: httpx.AsyncClient) -> None:
    client = await dcr(http)
    resp = await decide(http, await begin(http, client), "deny")
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(REDIRECT)
    assert "error=access_denied" in location
    assert "code" not in query(location)


async def test_code_is_single_use(http: httpx.AsyncClient) -> None:
    client = await dcr(http)
    code = await approve_flow(http, client)
    assert (await exchange(http, client, code)).status_code == 200
    second = await exchange(http, client, code)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"


async def test_expired_approval_request_is_400(
    http: httpx.AsyncClient, provider: GatewayAuthProvider, clock: FakeClock
) -> None:
    client = await dcr(http)
    request_id = await begin(http, client)
    clock.advance(provider.config.auth_code_ttl_s + 1)
    page = await http.get("/approve", params={"request": request_id})
    assert page.status_code == 400
    post = await decide(http, request_id, "approve")
    assert post.status_code == 400
    assert "location" not in post.headers


async def test_headless_client_cannot_use_authorize(
    http: httpx.AsyncClient, provider: GatewayAuthProvider
) -> None:
    resp = await authorize(http, "personal-assistant")
    location = resp.headers.get("location", "")
    assert "/approve" not in location
    assert resp.status_code == 400 or (
        resp.status_code == 302 and "unauthorized_client" in location
    )
    client = await provider.get_client("personal-assistant")
    assert client is not None
    params = AuthorizationParams(
        state=None,
        scopes=None,
        code_challenge=CHALLENGE,
        redirect_uri=AnyUrl(REDIRECT),
        redirect_uri_provided_explicitly=True,
    )
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client, params)
    assert exc.value.error == "unauthorized_client"


async def test_dcr_cannot_request_scope_outside_interactive_grant(http: httpx.AsyncClient) -> None:
    body = {
        "redirect_uris": [REDIRECT],
        "client_name": "Greedy",
        "token_endpoint_auth_method": "none",
        "scope": "bank:read",
    }
    resp = await http.post("/register", json=body)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


async def test_refresh_rotates_and_keeps_session_expiry(
    http: httpx.AsyncClient, provider: GatewayAuthProvider, clock: FakeClock
) -> None:
    client, first = await session(http)
    info = await provider.get_client(client["client_id"])
    assert info is not None
    old = await provider.load_refresh_token(info, first["refresh_token"])
    assert old is not None
    clock.advance(600)
    resp = await refresh(http, client, first["refresh_token"])
    assert resp.status_code == 200, resp.text
    second = resp.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert second["scope"] == INTERACTIVE
    replay = await refresh(http, client, first["refresh_token"])
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    new = await provider.load_refresh_token(info, second["refresh_token"])
    assert new is not None
    assert new.expires_at == old.expires_at


async def test_refresh_fails_after_session_ttl(
    http: httpx.AsyncClient, provider: GatewayAuthProvider, clock: FakeClock
) -> None:
    client, first = await session(http)
    clock.advance(provider.config.refresh_token_ttl_s + 1)
    resp = await refresh(http, client, first["refresh_token"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


async def test_client_credentials_issues_assistant_scopes(
    http: httpx.AsyncClient, provider: GatewayAuthProvider
) -> None:
    resp = await client_credentials(http, "personal-assistant", PA_SECRET)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"].split() == ASSISTANT_SCOPES
    assert "refresh_token" not in body
    assert body["expires_in"] == 3600
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
    access = await provider.load_access_token(body["access_token"])
    assert access is not None
    assert access.client_id == "personal-assistant"
    assert access.claims == {"client_name": "personal-assistant"}


async def test_client_credentials_accepts_basic_auth(http: httpx.AsyncClient) -> None:
    basic = base64.b64encode(f"personal-assistant:{PA_SECRET}".encode()).decode()
    resp = await http.post(
        "/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"].split() == ASSISTANT_SCOPES


async def test_client_credentials_wrong_secret_is_401(http: httpx.AsyncClient) -> None:
    resp = await client_credentials(http, "personal-assistant", "wrong")
    assert resp.status_code == 401
    assert resp.json() == INVALID_CLIENT


async def test_client_credentials_unknown_client_is_401(http: httpx.AsyncClient) -> None:
    wrong = await client_credentials(http, "personal-assistant", "wrong")
    for client_id in ("nobody", "interactive-default"):
        resp = await client_credentials(http, client_id, PA_SECRET)
        assert resp.status_code == wrong.status_code == 401
        assert resp.json() == wrong.json() == INVALID_CLIENT
    missing = await http.post("/token", data={"grant_type": "client_credentials"})
    assert missing.status_code == 401
    assert missing.json() == INVALID_CLIENT
    garbled = await http.post(
        "/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": "Basic !!!not-base64"},
    )
    assert garbled.status_code == 401
    assert garbled.json() == INVALID_CLIENT


async def test_revoke_access_token(http: httpx.AsyncClient, provider: GatewayAuthProvider) -> None:
    client, tokens = await session(http)
    resp = await http.post(
        "/revoke",
        # SDK 1.30 quirk: RevocationRequest.client_secret has no default, so it must be sent
        data={
            "token": tokens["access_token"],
            "client_id": client["client_id"],
            "client_secret": "",
        },
    )
    assert resp.status_code == 200, resp.text
    assert await provider.load_access_token(tokens["access_token"]) is None


async def test_revoke_via_refresh_token_kills_both(
    http: httpx.AsyncClient, provider: GatewayAuthProvider
) -> None:
    client, tokens = await session(http)
    resp = await http.post(
        "/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": client["client_id"],
            "client_secret": "",
        },
    )
    assert resp.status_code == 200, resp.text
    assert await provider.load_access_token(tokens["access_token"]) is None
    assert (await refresh(http, client, tokens["refresh_token"])).status_code == 400


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
