"""Helpers for driving the MCP SDK's own OAuth client against the gateway in-process."""

import httpx
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .conftest import PASSPHRASE
from .oauth_helpers import REDIRECT, query


class MemoryTokenStorage:
    """An in-memory ``TokenStorage``."""

    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


class NoStandaloneStream(httpx.AsyncBaseTransport):
    """Answers ``GET /mcp`` with 405 (no server-initiated stream) and forwards the rest.

    ``ASGITransport`` buffers a whole response, so the long-lived GET stream would never
    return, and ``OAuthClientProvider`` holds its lock across each request it signs: every
    later request would wait forever. Over a real socket the headers return at once.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/mcp":
            return httpx.Response(405, request=request)
        return await self._inner.handle_async_request(request)


class BrowserApproval:
    """Plays the user's browser: opens /authorize, approves, captures the callback."""

    def __init__(self, gw: httpx.AsyncClient) -> None:
        self._gw = gw
        self.callback: tuple[str, str | None] | None = None

    async def redirect_handler(self, authorization_url: str) -> None:
        resp = await self._gw.get(authorization_url, follow_redirects=False)
        assert resp.status_code == 302, resp.text
        approve_url = resp.headers["location"]
        page = await self._gw.get(approve_url)
        assert page.status_code == 200, page.text
        form = {
            "request": query(approve_url)["request"][0],
            "passphrase": PASSPHRASE,
            "decision": "approve",
        }
        done = await self._gw.post("/approve", data=form, follow_redirects=False)
        assert done.status_code == 303, done.text
        location = done.headers["location"]
        assert location.startswith(REDIRECT)
        params = query(location)
        self.callback = (params["code"][0], params.get("state", [None])[0])

    async def callback_handler(self) -> tuple[str, str | None]:
        assert self.callback is not None
        return self.callback
