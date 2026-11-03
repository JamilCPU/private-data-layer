"""In-process backend exposing the audit log to agents as the ``audit_search`` tool."""

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

import mcp.types

from pdl_gateway.audit import AuditLog

_SINCE_ERROR = "since must be an ISO 8601 timestamp"
_MAX_LIMIT = 500
_DEFAULT_LIMIT = 100

_DESCRIPTION = (
    "Search the gateway audit log of every tool call by every agent. Use it when the owner asks "
    "what an agent read, or when. Arguments: since (ISO 8601, required), client_id (optional), "
    "limit (1-500, default 100)."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "since": {"type": "string", "description": "ISO 8601 timestamp; earliest call to return"},
        "client_id": {"type": "string", "description": "Only calls made by this OAuth client"},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT},
    },
    "required": ["since"],
}


def _error(message: str) -> mcp.types.CallToolResult:
    return mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=message)], isError=True
    )


def _parse_limit(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= _MAX_LIMIT:
        return None
    return raw


class AuditBackend:
    name = "gateway"
    scope = "gateway:audit"

    def __init__(self, log: AuditLog) -> None:
        self._log = log

    async def list_tools(self) -> list[mcp.types.Tool]:
        return [mcp.types.Tool(name="audit_search", description=_DESCRIPTION, inputSchema=_SCHEMA)]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> mcp.types.CallToolResult:
        if name != "audit_search":
            return _error(f"unknown tool: {name}")
        try:
            since = datetime.fromisoformat(str(arguments["since"]))
        except (KeyError, ValueError, OverflowError):
            return _error(_SINCE_ERROR)
        limit = _parse_limit(arguments.get("limit", _DEFAULT_LIMIT))
        if limit is None:
            return _error(f"limit must be an integer between 1 and {_MAX_LIMIT}")
        client_id = arguments.get("client_id")
        try:
            entries = await asyncio.to_thread(
                self._log.search, since, client_id=client_id, limit=limit
            )
        except (ValueError, OverflowError):
            return _error(_SINCE_ERROR)
        payload = {"entries": [asdict(entry) for entry in entries]}
        return mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text=json.dumps(payload))],
            structuredContent=payload,
            isError=False,
        )
