"""Shared fixtures: an in-process stub of the MCP gateway (OAuth endpoints plus /mcp)."""

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from asgi_lifespan import LifespanManager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

GATEWAY_URL = "http://gateway.test"
PA_SECRET = "pa-secret"


@dataclass
class StubStats:
    """What the stub gateway saw (mutable on purpose: the stub records into it)."""

    token_requests: int = 0
    mcp_requests: int = 0
    rejected: int = 0
    auth_headers: list[str] = field(default_factory=list)
    registrations: list[dict[str, Any]] = field(default_factory=list)
    authorize_params: list[dict[str, str]] = field(default_factory=list)
    codes: dict[str, dict[str, str]] = field(default_factory=dict)


class _BearerGuard:
    """Require ``Authorization: Bearer <token>`` on /mcp, like the real gateway."""

    def __init__(self, app: ASGIApp, *, token: str, stats: StubStats, fail_first: bool) -> None:
        self.app = app
        self.token = token
        self.stats = stats
        self.fail_first = fail_first

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        self.stats.mcp_requests += 1
        self.stats.auth_headers.append(auth)
        forced = self.fail_first and self.stats.mcp_requests == 1
        if forced or auth != f"Bearer {self.token}":
            self.stats.rejected += 1
            deny = JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
            await deny(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _code_grant(form: Any, token: str, stats: StubStats) -> Response:
    issued = stats.codes.pop(str(form.get("code", "")), None)
    ok = (
        issued is not None
        and form.get("client_id") == issued["client_id"]
        and form.get("redirect_uri") == issued["redirect_uri"]
        and _s256(str(form.get("code_verifier", ""))) == issued["code_challenge"]
    )
    if not ok:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    body = {
        "access_token": f"interactive-{token}",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "homestudio:read",
        "refresh_token": "r-1",
    }
    return JSONResponse(body)


def _token_route(token: str, secret: str, stats: StubStats) -> Route:
    async def issue(request: Request) -> Response:
        stats.token_requests += 1
        form = await request.form()
        if form.get("grant_type") == "authorization_code":
            return _code_grant(form, token, stats)
        ok = (
            form.get("grant_type") == "client_credentials"
            and form.get("client_id") == "personal-assistant"
            and form.get("client_secret") == secret
        )
        if not ok:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        body = {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "homestudio:read",
        }
        return JSONResponse(body)

    return Route("/token", issue, methods=["POST"])


def _oauth_routes(stats: StubStats) -> list[Route]:
    """``/register`` (DCR) and ``/authorize`` (auto-approves: 302 back with a code)."""

    async def register(request: Request) -> Response:
        body = await request.json()
        stats.registrations.append(body)
        client_id = f"client-{secrets.token_hex(4)}"
        return JSONResponse({**body, "client_id": client_id}, status_code=201)

    async def authorize(request: Request) -> Response:
        params = dict(request.query_params)
        stats.authorize_params.append(params)
        code = secrets.token_urlsafe(16)
        stats.codes[code] = {
            "client_id": params["client_id"],
            "redirect_uri": params["redirect_uri"],
            "code_challenge": params["code_challenge"],
        }
        query = urlencode({"code": code, "state": params["state"]})
        return RedirectResponse(f"{params['redirect_uri']}?{query}", status_code=302)

    return [
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET"]),
    ]


def stub_gateway_app(
    tools: dict[str, Callable[..., Any]],
    *,
    token: str = "t-1",
    secret: str = PA_SECRET,
    fail_first_mcp_401: bool = False,
) -> Starlette:
    """A stub gateway: DCR, ``/authorize``, ``/token`` and a guarded FastMCP ``/mcp``."""
    server = FastMCP(
        "gateway",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    for tool_name, fn in tools.items():
        server.add_tool(fn, name=tool_name)
    mcp_app = server.streamable_http_app()
    stats = StubStats()
    guard = Middleware(_BearerGuard, token=token, stats=stats, fail_first=fail_first_mcp_401)
    app = Starlette(
        routes=[_token_route(token, secret, stats), *_oauth_routes(stats), *mcp_app.routes],
        middleware=[guard],
        lifespan=lambda _app: server.session_manager.run(),
    )
    app.state.stats = stats
    return app


def get_taste_profile() -> dict:
    """The user's taste profile."""
    return {"genres": ["jazz", "ambient"]}


def get_listening_history(since: str | None = None, limit: int = 50) -> list[dict]:
    """Recently played tracks, newest first."""
    return [{"track": "So What", "since": since}][:limit]


def explode() -> dict:
    """Always fails."""
    raise RuntimeError("backend exploded")


STUB_TOOLS: dict[str, Callable[..., Any]] = {
    "homestudio_get_taste_profile": get_taste_profile,
    "homestudio_get_listening_history": get_listening_history,
    "homestudio_explode": explode,
}


@asynccontextmanager
async def serve_gateway(
    tools: dict[str, Callable[..., Any]] | None = None, **kwargs: Any
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    """Run a stub gateway with its lifespan; yield it and an httpx client bound to it."""
    app = stub_gateway_app(STUB_TOOLS if tools is None else tools, **kwargs)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http,
    ):
        yield app, http


@pytest.fixture
async def gateway() -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    async with serve_gateway() as served:
        yield served
