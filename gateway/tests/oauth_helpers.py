"""Shared helpers for the OAuth route tests: DCR, PKCE, approval, token requests."""

import base64
import hashlib
from secrets import token_urlsafe
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .conftest import PASSPHRASE

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


async def exchange(
    http: httpx.AsyncClient, client: dict[str, Any], code: str, verifier: str = VERIFIER
) -> httpx.Response:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client["client_id"],
        "code_verifier": verifier,
    }
    return await http.post("/token", data=form)


async def refresh(
    http: httpx.AsyncClient, client: dict[str, Any], token: str, **extra: str
) -> httpx.Response:
    form = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": client["client_id"],
        **extra,
    }
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
