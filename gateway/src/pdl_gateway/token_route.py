"""``/token`` endpoint: our ``client_credentials`` grant, everything else via the SDK handler."""

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from urllib.parse import unquote

from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.provider import TokenError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from pdl_gateway.oauth import GatewayAuthProvider
from pdl_gateway.throttle import AttemptThrottle

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_INVALID_CLIENT = {"error": "invalid_client", "error_description": "unknown client or wrong secret"}


def _invalid_client() -> JSONResponse:
    return JSONResponse(_INVALID_CLIENT, status_code=401, headers=_NO_STORE)


def _locked(throttle: AttemptThrottle) -> JSONResponse:
    body = {"error": "invalid_client", "error_description": "too many failed attempts"}
    headers = {**_NO_STORE, "Retry-After": str(round(throttle.window_s))}
    return JSONResponse(body, status_code=429, headers=headers)


def _basic_credentials(header: str) -> tuple[str, str] | None:
    try:
        decoded = base64.b64decode(header[len("Basic ") :], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    client_id, sep, client_secret = decoded.partition(":")
    return (unquote(client_id), unquote(client_secret)) if sep else None


def _credentials(form: FormData, authorization: str) -> tuple[str, str] | None:
    """Client id and secret from HTTP Basic, else from the form; None if absent."""
    if authorization.startswith("Basic "):
        return _basic_credentials(authorization)
    client_id, client_secret = form.get("client_id"), form.get("client_secret")
    if isinstance(client_id, str) and isinstance(client_secret, str) and client_id:
        return client_id, client_secret
    return None


def build_token_endpoint(
    provider: GatewayAuthProvider, sdk_handler: TokenHandler, throttle: AttemptThrottle
) -> Callable[[Request], Awaitable[Response]]:
    """Return the ``/token`` request handler.

    ``client_credentials`` checks are serialised behind ``throttle`` (as ``/approve`` is),
    so concurrent guesses cannot overrun the lockout.
    """
    lock = asyncio.Lock()

    async def client_credentials(credentials: tuple[str, str]) -> Response:
        async with lock:
            if throttle.blocked():
                return _locked(throttle)
            try:
                token = await run_in_threadpool(provider.issue_client_credentials, *credentials)
            except TokenError:
                throttle.record_failure()
                return _invalid_client()
        return JSONResponse(token.model_dump(exclude_none=True), headers=_NO_STORE)

    async def token_endpoint(request: Request) -> Response:
        form = await request.form()  # Starlette caches it, so the SDK can re-read it
        if form.get("grant_type") != "client_credentials":
            return await sdk_handler.handle(request)
        if throttle.blocked():
            return _locked(throttle)
        credentials = _credentials(form, request.headers.get("Authorization", ""))
        if credentials is None:
            return _invalid_client()
        return await client_credentials(credentials)

    return token_endpoint
