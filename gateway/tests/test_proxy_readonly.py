"""Read-only enforcement: backend tools that declare writes are never exposed or callable."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import mcp.types
import pytest
from asgi_lifespan import LifespanManager
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from pdl_gateway.audit import AuditLog
from pdl_gateway.backends import RemoteBackend
from pdl_gateway.proxy import Caller, build_proxy_server

from .conftest import STUB_URL, FakeClock, gateway_session

HOME = Caller("agent-1", None, frozenset({"homestudio:read"}))
WRITES = Caller("agent-5", None, frozenset({"writes:read"}))


def _hidden_logs(caplog: pytest.LogCaptureFixture, tool: str) -> int:
    """How many times the proxy logged hiding ``tool``."""
    return sum(
        1
        for r in caplog.records
        if r.name == "pdl_gateway.proxy"
        and "not read-only" in r.getMessage()
        and tool in r.getMessage()
    )


def list_queue() -> dict:
    return {"queue": []}


def clear_queue() -> dict:  # pragma: no cover - must never be reached through the gateway
    return {"cleared": True}


def get_status() -> dict:  # pragma: no cover - only listed, never called
    return {"ok": True}


@pytest.fixture
async def mixed_stub(asgi_factory) -> AsyncIterator[RemoteBackend]:
    """A real FastMCP backend with an unannotated, a read-only and a destructive tool."""
    server = FastMCP(
        "mixed",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    server.add_tool(list_queue, name="list_queue")
    server.add_tool(get_status, name="get_status", annotations=ToolAnnotations(readOnlyHint=True))
    server.add_tool(
        clear_queue, name="clear_queue", annotations=ToolAnnotations(destructiveHint=True)
    )
    app = server.streamable_http_app()
    async with LifespanManager(app):
        yield RemoteBackend(
            "homestudio", "homestudio:read", STUB_URL, {}, httpx_client_factory=asgi_factory(app)
        )


async def test_destructive_tool_is_hidden_and_unknown(
    mixed_stub: RemoteBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="pdl_gateway.proxy")
    server = build_proxy_server([mixed_stub], AuditLog(tmp_path / "audit"))
    async with gateway_session(server, HOME, monkeypatch) as session:
        tools = (await session.list_tools()).tools
        result = await session.call_tool("homestudio_clear_queue", {})

    assert [t.name for t in tools] == ["homestudio_get_status", "homestudio_list_queue"]
    assert all(t.annotations and t.annotations.readOnlyHint is True for t in tools)
    assert result.isError is True
    block = result.content[0]
    assert isinstance(block, mcp.types.TextContent)
    assert block.text == "unknown tool: homestudio_clear_queue"
    assert _hidden_logs(caplog, "clear_queue") == 1


class WritesBackend:
    """An in-process backend whose tool says it is not read-only."""

    name = "writes"
    scope = "writes:read"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_tools(self) -> list[mcp.types.Tool]:
        return [
            mcp.types.Tool(name="peek", inputSchema={"type": "object"}),
            mcp.types.Tool(
                name="poke",
                inputSchema={"type": "object"},
                annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
            ),
        ]

    async def call_tool(  # pragma: no cover - the gateway must never call it
        self, name: str, arguments: dict[str, Any]
    ) -> mcp.types.CallToolResult:
        self.calls.append(name)
        return mcp.types.CallToolResult(content=[])


async def test_not_read_only_tool_is_dropped_and_logged_once_per_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="pdl_gateway.proxy")
    backend = WritesBackend()
    clock = FakeClock(1000.0)
    server = build_proxy_server(
        [backend], AuditLog(tmp_path / "audit"), tools_cache_ttl_s=60.0, clock=clock
    )
    async with gateway_session(server, WRITES, monkeypatch) as session:
        assert [t.name for t in (await session.list_tools()).tools] == ["writes_peek"]
        assert (await session.call_tool("writes_poke", {})).isError is True
        assert _hidden_logs(caplog, "poke") == 1
        clock.advance(61.0)
        await session.list_tools()
        assert _hidden_logs(caplog, "poke") == 2
    assert backend.calls == []
