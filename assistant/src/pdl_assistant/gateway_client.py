"""Client of the MCP gateway: client-credentials token, then MCP over streamable HTTP."""

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeVar

import anyio
import httpx
import pydantic
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ClientRequest,
    PaginatedRequestParams,
    TextContent,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Seconds before a token's stated expiry at which it is no longer reused.
_EXPIRY_MARGIN_S = 60

# Failures that mean "the gateway could not be reached or did not speak MCP properly".
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    McpError,
    pydantic.ValidationError,
    OSError,
    TimeoutError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
)


class GatewayUnavailable(Exception):
    """The gateway could not be reached, timed out, refused our credentials or broke protocol."""


class _Unauthorized(Exception):
    """/mcp answered 401: the access token is expired or revoked."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool
    structured: dict | None


def to_ollama(spec: ToolSpec) -> dict:
    """The tool in Ollama's ``tools`` format."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        },
    }


class _ViaClient(httpx.AsyncBaseTransport):
    """Send requests through another ``httpx.AsyncClient`` (shares its transport and pool)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._client.send(request, stream=True)


def _leaves(exc: BaseException) -> list[BaseException]:
    """The non-group exceptions inside ``exc`` (``exc`` itself when it is not a group)."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _is_401(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401


def _reason(exc: BaseException) -> str:
    """A short, single-line human reason for a transport failure."""
    if isinstance(exc, McpError):
        text = exc.error.message
    elif isinstance(exc, TimeoutError):
        text = "timed out"
    else:
        text = str(exc) or type(exc).__name__
    return (text.strip().splitlines() or [type(exc).__name__])[0]


def _translate(exc: BaseException) -> BaseException:
    """Map a session failure to _Unauthorized, GatewayUnavailable, or the original error."""
    leaves = _leaves(exc)
    if any(_is_401(leaf) for leaf in leaves):
        return _Unauthorized()
    if all(isinstance(leaf, _TRANSPORT_ERRORS) for leaf in leaves):
        reason = _reason(leaves[0])
        logger.warning("gateway unavailable: %s", reason)
        return GatewayUnavailable(reason)
    if len(leaves) == 1:
        return leaves[0]  # surface a lone wrapped error as itself
    return exc


def _tool_result(result: CallToolResult) -> ToolResult:
    text = "".join(block.text for block in result.content if isinstance(block, TextContent))
    return ToolResult(text=text, is_error=bool(result.isError), structured=result.structuredContent)


class GatewayClient:
    """The headless ``personal-assistant`` client of the MCP gateway."""

    def __init__(
        self,
        gateway_url: str,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.AsyncClient | None = None,
        tool_timeout_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        base = gateway_url.rstrip("/")
        self._token_url = f"{base}/token"
        self._mcp_url = f"{base}/mcp"
        self._client_id = client_id
        self._client_secret = client_secret
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient()
        self._timeout_s = tool_timeout_s
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    async def token(self) -> str:
        """A valid access token, fetched with client credentials and cached until near expiry."""
        if self._token is not None and self._clock() < self._expires_at:
            return self._token
        self._token = None
        token, expires_in = await self._fetch_token()
        self._token = token
        self._expires_at = self._clock() + max(expires_in - _EXPIRY_MARGIN_S, 0)
        return token

    async def _fetch_token(self) -> tuple[str, float]:
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            response = await self._http.post(self._token_url, data=form, timeout=self._timeout_s)
        except (httpx.HTTPError, RuntimeError) as exc:  # RuntimeError: client already closed
            raise GatewayUnavailable(f"token endpoint: {_reason(exc)}") from exc
        if response.status_code != 200:
            raise GatewayUnavailable(f"token endpoint answered {response.status_code}")
        try:
            body = response.json()
            return str(body["access_token"]), float(body.get("expires_in", 0))
        except (ValueError, KeyError, TypeError) as exc:
            raise GatewayUnavailable("token endpoint sent a malformed reply") from exc

    @asynccontextmanager
    async def _session(self, token: str) -> AsyncIterator[ClientSession]:
        """A fresh, initialized MCP session authenticated with ``token``."""
        client = httpx.AsyncClient(
            transport=_ViaClient(self._http),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(self._timeout_s),
        )
        read_timeout = timedelta(seconds=self._timeout_s)
        try:
            with anyio.fail_after(self._timeout_s):
                async with (
                    client,
                    streamable_http_client(self._mcp_url, http_client=client) as (read, write, _),
                    ClientSession(read, write, read_timeout_seconds=read_timeout) as session,
                ):
                    await session.initialize()
                    yield session
        except BaseException as exc:
            translated = _translate(exc)
            if translated is exc:
                raise
            raise translated from exc

    async def _run(self, operation: Callable[[ClientSession], Awaitable[T]]) -> T:
        """Run ``operation`` in a session; on a 401 refresh the token and retry once."""
        for attempt in range(2):
            token = await self.token()
            try:
                async with self._session(token) as session:
                    return await operation(session)
            except _Unauthorized:
                logger.info("gateway rejected the access token (attempt %d)", attempt + 1)
                self._token = None
        raise GatewayUnavailable("gateway rejected a freshly issued access token")

    async def list_tools(self) -> list[ToolSpec]:
        """Every tool the gateway offers this client, sorted by name."""

        async def operation(session: ClientSession) -> list[ToolSpec]:
            specs: list[ToolSpec] = []
            cursor: str | None = None
            while True:
                params = PaginatedRequestParams(cursor=cursor) if cursor else None
                page = await session.list_tools(params=params)
                specs.extend(
                    ToolSpec(t.name, t.description or "", dict(t.inputSchema)) for t in page.tools
                )
                cursor = page.nextCursor
                if not cursor:
                    return sorted(specs, key=lambda s: s.name)

        return await self._run(operation)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Call ``name`` through the gateway; tool-level failures come back as ``is_error``."""
        # A raw tools/call: ClientSession.call_tool would add a hidden tools/list per session.
        request = ClientRequest(
            CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))
        )

        async def operation(session: ClientSession) -> ToolResult:
            return _tool_result(await session.send_request(request, CallToolResult))

        return await self._run(operation)
