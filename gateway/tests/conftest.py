"""Shared fixtures: config, secrets, store, fake clock, OAuth provider, auth app, stub backends."""

import dataclasses
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared._httpx_utils import McpHttpClientFactory
from starlette.applications import Starlette
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
STUB_URL = "http://stub/mcp"


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


class RequestRecorder:
    """ASGI middleware recording each HTTP request's headers and JSON-RPC method."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.requests: list[dict[str, str]] = []
        self.methods: list[str] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.requests.append({k.decode(): v.decode() for k, v in scope["headers"]})
        chunks: list[bytes] = []

        async def recording_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request" and scope["method"] == "POST":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    self.methods.append(json.loads(b"".join(chunks))["method"])
            return message

        await self.app(scope, recording_receive, send)


def stub_backend_app(tools: dict[str, Callable[..., Any]], name: str = "stub") -> Starlette:
    """A stateless JSON-response FastMCP server exposing ``tools``, as a Starlette app."""
    server = FastMCP(
        name,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    for tool_name, fn in tools.items():
        server.add_tool(fn, name=tool_name)
    return server.streamable_http_app()


def get_taste_profile() -> dict:
    return {"genres": ["jazz", "ambient"]}


def list_queue() -> dict:
    return {"queue": ["track-1", "track-2"]}


def list_devices() -> list:
    return [{"device": "pi1"}, {"device": "pi2"}]


def reboot_device(device: str) -> dict:
    return {"rebooted": device}


def recent_history(limit: int = 20) -> list:
    return [{"event": "boot"}][:limit]


async def serve_stub(
    tools: dict[str, Callable[..., Any]], name: str
) -> AsyncIterator[RequestRecorder]:
    """Run a stub backend with its lifespan, yielding the recording ASGI app."""
    recorder = RequestRecorder(stub_backend_app(tools, name))
    async with LifespanManager(recorder):
        yield recorder


@pytest.fixture
async def homestudio_stub() -> AsyncIterator[RequestRecorder]:
    tools = {"get_taste_profile": get_taste_profile, "list_queue": list_queue}
    async for app in serve_stub(tools, "homestudio"):
        yield app


@pytest.fixture
async def automations_stub() -> AsyncIterator[RequestRecorder]:
    tools = {
        "list_devices": list_devices,
        "reboot_device": reboot_device,
        "recent_history": recent_history,
    }
    async for app in serve_stub(tools, "automations"):
        yield app


def _asgi_factory(app: ASGIApp) -> McpHttpClientFactory:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://stub",
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    return factory


@pytest.fixture
def asgi_factory() -> Callable[[ASGIApp], McpHttpClientFactory]:
    """``asgi_factory(app)`` returns an MCP client factory that talks to ``app`` in-process."""
    return _asgi_factory
