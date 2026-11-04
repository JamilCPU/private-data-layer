"""Tests for the gateway client: client-credentials token, MCP tool listing and calls."""

import httpx
import pytest
from starlette.applications import Starlette

from pdl_assistant.gateway_client import (
    GatewayClient,
    GatewayUnavailable,
    ToolResult,
    ToolSpec,
    to_ollama,
)

from .conftest import GATEWAY_URL, PA_SECRET, serve_gateway

Served = tuple[Starlette, httpx.AsyncClient]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _client(http: httpx.AsyncClient, secret: str = PA_SECRET, **kwargs) -> GatewayClient:
    return GatewayClient(GATEWAY_URL, "personal-assistant", secret, http=http, **kwargs)


def _token_reply(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/token":
        return httpx.Response(200, json={"access_token": "t-1", "expires_in": 3600})
    return None


async def test_token_cached_and_refreshed_on_401() -> None:
    clock = FakeClock()
    async with serve_gateway(fail_first_mcp_401=True) as (app, http):
        client = _client(http, clock=clock)
        stats = app.state.stats

        tools = await client.list_tools()  # first /mcp request is refused -> refresh, retry
        assert tools
        assert stats.rejected == 1
        assert stats.token_requests == 2
        assert set(stats.auth_headers) == {"Bearer t-1"}

        await client.call_tool("homestudio_get_taste_profile", {})
        assert stats.token_requests == 2, "a valid token is reused"

        clock.now += 3600 - 60  # within 60 s of expiry: fetch a fresh one
        assert await client.token() == "t-1"
        assert stats.token_requests == 3


async def test_persistent_401_retries_once_then_unavailable() -> None:
    calls = {"token": 0, "mcp": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        reply = _token_reply(request)
        calls["token" if reply is not None else "mcp"] += 1
        return reply or httpx.Response(401, headers={"WWW-Authenticate": "Bearer"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GatewayUnavailable, match="rejected"):
            await _client(http).list_tools()
    assert calls == {"token": 2, "mcp": 2}


async def test_list_tools_maps_schema(gateway: Served) -> None:
    _, http = gateway
    tools = await _client(http).list_tools()

    assert [t.name for t in tools] == sorted(t.name for t in tools)
    history = next(t for t in tools if t.name == "homestudio_get_listening_history")
    assert isinstance(history, ToolSpec)
    assert history.description == "Recently played tracks, newest first."
    assert set(history.input_schema["properties"]) == {"since", "limit"}

    assert to_ollama(history) == {
        "type": "function",
        "function": {
            "name": "homestudio_get_listening_history",
            "description": "Recently played tracks, newest first.",
            "parameters": history.input_schema,
        },
    }


async def test_call_tool_passthrough_and_error_flag(gateway: Served) -> None:
    _, http = gateway
    client = _client(http)

    ok = await client.call_tool("homestudio_get_listening_history", {"since": "2026-11-01"})
    assert isinstance(ok, ToolResult)
    assert ok.is_error is False
    assert '"So What"' in ok.text
    assert ok.structured == {"result": [{"track": "So What", "since": "2026-11-01"}]}

    bad = await client.call_tool("homestudio_explode", {})
    assert bad.is_error is True
    assert "backend exploded" in bad.text


async def test_unavailable_raises(gateway: Served) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as http:
        with pytest.raises(GatewayUnavailable):
            await _client(http).token()

    def mcp_down(request: httpx.Request) -> httpx.Response:
        reply = _token_reply(request)
        if reply is None:
            raise httpx.ConnectError("connection refused", request=request)
        return reply

    async with httpx.AsyncClient(transport=httpx.MockTransport(mcp_down)) as http:
        with pytest.raises(GatewayUnavailable):
            await _client(http).call_tool("homestudio_get_taste_profile", {})

    _, stub_http = gateway
    with pytest.raises(GatewayUnavailable, match="401"):
        await _client(stub_http, secret="wrong").list_tools()


async def test_owned_client_is_closed() -> None:
    client = GatewayClient(GATEWAY_URL, "personal-assistant", PA_SECRET)
    await client.aclose()
    with pytest.raises(GatewayUnavailable):
        await client.token()


def test_text_blocks_are_joined_with_newlines() -> None:
    from mcp.types import CallToolResult, TextContent

    from pdl_assistant.gateway_client import _tool_result

    result = CallToolResult(
        content=[TextContent(type="text", text="first"), TextContent(type="text", text="second")]
    )
    assert _tool_result(result) == ToolResult(text="first\nsecond", is_error=False, structured=None)
