"""Entry point: serve the search MCP server over streamable HTTP behind the shared secret."""

from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

import uvicorn

from pdl_search.middleware import SharedSecretMiddleware
from pdl_search.searx import SearxClient
from pdl_search.server import build_server

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


def _error(message: str) -> int:
    print(f"search-mcp: {message}", file=sys.stderr)
    return 1


def _is_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def main(argv: list[str] | None = None) -> int:
    del argv  # configuration comes from the environment only
    secret = os.environ.get("SEARCH_MCP_SHARED_SECRET", "").strip()
    if not secret:
        return _error("environment variable SEARCH_MCP_SHARED_SECRET is required")
    searx_url = os.environ.get("SEARCH_SEARXNG_URL", "").strip()
    if not searx_url:
        return _error("environment variable SEARCH_SEARXNG_URL is required")
    if not _is_http_url(searx_url):
        return _error(f"SEARCH_SEARXNG_URL must be an http(s) URL, got {searx_url!r}")
    host = os.environ.get("SEARCH_MCP_HOST", "").strip() or DEFAULT_HOST
    raw_port = os.environ.get("SEARCH_MCP_PORT", "").strip() or str(DEFAULT_PORT)
    try:
        port = int(raw_port)
    except ValueError:
        return _error(f"SEARCH_MCP_PORT must be an integer, got {raw_port!r}")
    server = build_server(SearxClient(searx_url), host=host, port=port)
    uvicorn.run(SharedSecretMiddleware(server.streamable_http_app(), secret), host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
