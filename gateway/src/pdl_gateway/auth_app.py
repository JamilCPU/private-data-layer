"""Assemble the authorization-server and protected-resource-metadata routes."""

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import (
    TOKEN_PATH,
    _body_limited,
    _cors,
    build_metadata,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.routing import Route, request_response

from pdl_gateway.approval import build_approval_routes
from pdl_gateway.config import GatewayConfig
from pdl_gateway.oauth import GatewayAuthProvider
from pdl_gateway.token_route import build_token_endpoint

METADATA_PATH = "/.well-known/oauth-authorization-server"
GRANT_TYPES = ["authorization_code", "refresh_token", "client_credentials"]
TOKEN_AUTH_METHODS = ["client_secret_post", "client_secret_basic", "none"]
RESOURCE_NAME = "private-data-layer gateway"


def _metadata_route(
    issuer: AnyHttpUrl, registration: ClientRegistrationOptions, revocation: RevocationOptions
) -> Route:
    metadata = build_metadata(issuer, None, registration, revocation).model_copy(
        update={
            "grant_types_supported": list(GRANT_TYPES),
            "token_endpoint_auth_methods_supported": list(TOKEN_AUTH_METHODS),
        }
    )
    return Route(
        METADATA_PATH,
        endpoint=cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
        methods=["GET", "OPTIONS"],
    )


def _token_route(provider: GatewayAuthProvider) -> Route:
    sdk_handler = TokenHandler(provider, ClientAuthenticator(provider))
    endpoint = request_response(build_token_endpoint(provider, sdk_handler))
    return Route(
        TOKEN_PATH,
        endpoint=_cors(_body_limited(endpoint), ["POST", "OPTIONS"]),
        methods=["POST", "OPTIONS"],
    )


def build_auth_routes(config: GatewayConfig, provider: GatewayAuthProvider) -> list[Route]:
    """Metadata, /authorize, /token (wrapped), /register, /revoke, /approve and PRM routes."""
    issuer = AnyHttpUrl(config.issuer_url)
    interactive = list(config.interactive_scopes)
    registration = ClientRegistrationOptions(
        enabled=True, valid_scopes=interactive, default_scopes=interactive
    )
    revocation = RevocationOptions(enabled=True)
    sdk_routes = create_auth_routes(
        provider,
        issuer,
        client_registration_options=registration,
        revocation_options=revocation,
    )
    replacements = {
        METADATA_PATH: _metadata_route(issuer, registration, revocation),
        TOKEN_PATH: _token_route(provider),
    }
    missing = set(replacements) - {route.path for route in sdk_routes}
    if missing:
        raise RuntimeError(f"MCP SDK auth routes changed; cannot replace {sorted(missing)}")
    return [
        *(replacements.get(route.path, route) for route in sdk_routes),
        *build_approval_routes(provider),
        *create_protected_resource_routes(
            AnyHttpUrl(config.resource_url),
            [issuer],
            scopes_supported=list(config.scopes),
            resource_name=RESOURCE_NAME,
        ),
    ]
