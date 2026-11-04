"""Redirect URI policy for DCR: loopback, or https under the gateway's own parent domain."""

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette

from pdl_gateway.auth_app import build_auth_routes
from pdl_gateway.config import GatewayConfig, GatewaySecrets
from pdl_gateway.oauth import GatewayAuthProvider, issuer_parent_domain
from pdl_gateway.store import TokenStore

from .oauth_helpers import REDIRECT

LAB_ISSUER = "https://gateway.lab"
REFUSAL = "redirect_uris must be loopback or https under the gateway's domain"


@pytest.fixture
async def lab_http(
    config: GatewayConfig, secrets: GatewaySecrets, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    lab_config = dataclasses.replace(
        config,
        issuer_url=LAB_ISSUER,
        resource_url=f"{LAB_ISSUER}/mcp",
        db_path=str(tmp_path / "lab.db"),
    )
    store = TokenStore(lab_config.db_path)
    provider = GatewayAuthProvider(lab_config, secrets, store)
    app = Starlette(routes=build_auth_routes(lab_config, provider))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=LAB_ISSUER
    ) as client:
        yield client
    store.close()


async def _register(http: httpx.AsyncClient, uri: str) -> httpx.Response:
    return await http.post(
        "/register",
        json={
            "redirect_uris": [uri],
            "client_name": "Assistant",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


async def test_same_domain_https_redirect_accepted(lab_http: httpx.AsyncClient) -> None:
    resp = await _register(lab_http, "https://assistant.lab/oauth/callback")
    assert resp.status_code == 201, resp.text


async def test_nested_subdomain_accepted(lab_http: httpx.AsyncClient) -> None:
    assert (await _register(lab_http, "https://x.y.lab/cb")).status_code == 201


async def test_same_domain_http_redirect_refused(lab_http: httpx.AsyncClient) -> None:
    resp = await _register(lab_http, "http://assistant.lab/cb")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"
    assert resp.json()["error_description"] == REFUSAL


async def test_other_domain_refused(lab_http: httpx.AsyncClient) -> None:
    resp = await _register(lab_http, "https://evil.lab.attacker.example/cb")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


async def test_userinfo_trick_refused(lab_http: httpx.AsyncClient) -> None:
    resp = await _register(lab_http, "https://assistant.lab@evil.example/cb")
    assert resp.status_code == 400


async def test_parent_domain_itself_refused(lab_http: httpx.AsyncClient) -> None:
    assert (await _register(lab_http, "https://lab/cb")).status_code == 400


async def test_loopback_still_accepted_with_lab_issuer(lab_http: httpx.AsyncClient) -> None:
    assert (await _register(lab_http, REDIRECT)).status_code == 201


async def test_localhost_issuer_accepts_only_loopback(http: httpx.AsyncClient) -> None:
    assert (await _register(http, "https://assistant.lab/cb")).status_code == 400
    assert (await _register(http, REDIRECT)).status_code == 201


@pytest.mark.parametrize(
    ("issuer", "parent"),
    [
        ("https://gateway.lab", "lab"),
        ("https://gw.home.example.com:8443/x", "home.example.com"),
        ("https://GATEWAY.Lab", "lab"),
        ("http://localhost:8787", None),
        ("https://lab", None),
        ("https://192.168.0.5", None),
        ("https://[::1]:8787", None),
    ],
)
def test_issuer_parent_domain(issuer: str, parent: str | None) -> None:
    assert issuer_parent_domain(issuer) == parent
