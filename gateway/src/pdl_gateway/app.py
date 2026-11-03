"""Assemble the gateway: OAuth routes, ``/healthz`` and the bearer-protected ``/mcp`` proxy."""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from pdl_gateway.audit import AuditLog
from pdl_gateway.audit_backend import AuditBackend
from pdl_gateway.auth_app import build_auth_routes
from pdl_gateway.backends import Backend
from pdl_gateway.config import ConfigError, GatewayConfig, GatewaySecrets
from pdl_gateway.oauth import GatewayAuthProvider
from pdl_gateway.proxy import build_proxy_server
from pdl_gateway.store import TokenStore

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"


def check_backend_names(names: Sequence[str]) -> None:
    """Refuse names that make an exposed ``<backend>_<tool>`` name ambiguous."""
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise ConfigError(f"duplicate backend name(s): {', '.join(map(repr, dups))}")
    clashes = sorted(
        (short, long) for short in names for long in names if long.startswith(f"{short}_")
    )
    if clashes:
        pairs = "; ".join(f"{short!r} and {long!r}" for short, long in clashes)
        raise ConfigError(f"backend names make tool names ambiguous: {pairs}")


async def _healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


def _mcp_route(config: GatewayConfig, manager: StreamableHTTPSessionManager) -> Route:
    """``/mcp`` behind the bearer check; a Route (not a Mount) so ``/mcp`` is not redirected."""
    resource = AnyHttpUrl(config.resource_url)
    protected = RequireAuthMiddleware(
        StreamableHTTPASGIApp(manager),
        required_scopes=[],
        resource_metadata_url=build_resource_metadata_url(resource),
    )
    return Route(MCP_PATH, endpoint=protected)


def _assemble(
    config: GatewayConfig,
    secrets: GatewaySecrets,
    exposed: Sequence[Backend],
    audit: AuditLog,
    store: TokenStore,
    owns_store: bool,
) -> Starlette:
    provider = GatewayAuthProvider(config, secrets, store)
    server = build_proxy_server(exposed, audit, tools_cache_ttl_s=config.tools_cache_ttl_s)
    manager = StreamableHTTPSessionManager(app=server)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            async with manager.run():
                logger.info("gateway serving %d backend(s)", len(exposed))
                yield
        finally:
            if owns_store:
                store.close()

    auth_backend = BearerAuthBackend(
        ProviderTokenVerifier(provider), resource_server_url=AnyHttpUrl(config.resource_url)
    )
    return Starlette(
        routes=[
            *build_auth_routes(config, provider),
            Route("/healthz", _healthz, methods=["GET"]),
            _mcp_route(config, manager),
        ],
        middleware=[
            Middleware(AuthenticationMiddleware, backend=auth_backend),
            Middleware(AuthContextMiddleware),
        ],
        lifespan=lifespan,
    )


def build_app(
    config: GatewayConfig,
    secrets: GatewaySecrets,
    backends: Sequence[Backend],
    *,
    store: TokenStore | None = None,
    audit: AuditLog | None = None,
) -> Starlette:
    """The gateway ASGI app; the audit backend is appended to ``backends``.

    Backend names are checked before anything is created; a store created here is closed
    if assembly fails, and on lifespan shutdown otherwise.
    """
    check_backend_names([*(b.name for b in backends), AuditBackend.name])
    audit = audit if audit is not None else AuditLog(Path(config.audit_dir))
    exposed = [*backends, AuditBackend(audit)]
    if store is not None:
        return _assemble(config, secrets, exposed, audit, store, owns_store=False)
    own_store = TokenStore(config.db_path)
    try:
        return _assemble(config, secrets, exposed, audit, own_store, owns_store=True)
    except BaseException:
        own_store.close()
        raise
