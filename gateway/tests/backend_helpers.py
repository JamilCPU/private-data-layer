"""Mock-transport helpers for the remote backend tests."""

import json
from collections.abc import Awaitable, Callable

import httpx
import mcp.types
from mcp.shared._httpx_utils import McpHttpClientFactory

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def mock_factory(handler: Handler) -> McpHttpClientFactory:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers)

    return factory


def fake_server(results: dict[str, object]) -> McpHttpClientFactory:
    """A mock MCP server: a valid initialize, then the given raw result per method."""
    init = {
        "protocolVersion": mcp.types.LATEST_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "fake", "version": "1"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        result = init if body["method"] == "initialize" else results[body["method"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    return mock_factory(handler)
