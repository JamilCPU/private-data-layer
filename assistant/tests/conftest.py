"""Shared fixtures: an in-process stub of the MCP gateway (token endpoint plus /mcp)."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
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


def _token_route(token: str, secret: str, stats: StubStats) -> Route:
    async def issue(request: Request) -> Response:
        stats.token_requests += 1
        form = await request.form()
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


def stub_gateway_app(
    tools: dict[str, Callable[..., Any]],
    *,
    token: str = "t-1",
    secret: str = PA_SECRET,
    fail_first_mcp_401: bool = False,
) -> Starlette:
    """A stub gateway: ``POST /token`` (client credentials) and a guarded FastMCP ``/mcp``."""
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
        routes=[_token_route(token, secret, stats), *mcp_app.routes],
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
async def serve_gateway(**kwargs: Any) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    """Run a stub gateway with its lifespan; yield it and an httpx client bound to it."""
    app = stub_gateway_app(STUB_TOOLS, **kwargs)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http,
    ):
        yield app, http


@pytest.fixture
async def gateway() -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    async with serve_gateway() as served:
        yield served
