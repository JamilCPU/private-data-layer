"""Front-door auth: signed session cookies, login via the gateway's approval page, bearer tokens.

Humans log in by passing the gateway's OAuth approval page (authorization code + PKCE). The
access token that comes back is discarded: it only proves the approval happened; tools run
under the assistant's own headless identity. Programmatic callers of the OpenAI-compatible
endpoint use static API tokens checked against PBKDF2 hashes.
"""

import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from itsdangerous import BadSignature, TimestampSigner

from .memory import MemoryStore
from .secrets import verify_secret

log = logging.getLogger(__name__)

CLIENT_ID_KEY = "oauth_client_id"
CLIENT_NAME = "private-data-layer assistant"
MAX_PENDING_LOGINS = 256
_ERROR_BODY_CHARS = 200
_SALT = "pdl-assistant-session"


class LoginError(Exception):
    """The gateway refused or could not complete a login step."""


class SessionSigner:
    """Signs session ids into cookie values; rejects tampered or older-than-``ttl_s`` cookies."""

    def __init__(self, secret: str, ttl_s: int) -> None:
        self._signer = TimestampSigner(secret, salt=_SALT)
        self._ttl_s = ttl_s

    def issue(self, session_id: str) -> str:
        return self._signer.sign(session_id).decode("ascii")

    def verify(self, cookie: str) -> str | None:
        try:
            return self._signer.unsign(cookie, max_age=self._ttl_s).decode("utf-8")
        except (BadSignature, UnicodeError):
            return None


def issue_session(signer: SessionSigner, store: MemoryStore) -> str:
    """Create a session in the store and return its signed cookie value."""
    return signer.issue(store.create_session())


def session_from_cookie(
    signer: SessionSigner, store: MemoryStore, cookie: str | None
) -> str | None:
    """The session id for a cookie that is authentic, younger than the TTL and not revoked."""
    if not cookie:
        return None
    session_id = signer.verify(cookie)
    if session_id is None or not store.session_valid(session_id):
        return None
    return session_id


def bearer_ok(header_value: str | None, hashes: Mapping[str, str]) -> bool:
    """True if ``Authorization: Bearer <token>`` matches any configured token hash."""
    if not header_value or not hashes:
        return False
    scheme, _, token = header_value.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return False
    matched = False
    for encoded in hashes.values():  # no early exit: every hash is checked
        matched = verify_secret(token, encoded) | matched
    return matched


@dataclass(frozen=True)
class _Pending:
    client_id: str
    verifier: str
    created_at: float


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _json_field(resp: httpx.Response, name: str) -> str | None:
    """A string field of a JSON object reply, or None if absent or the reply is not an object."""
    try:
        data = resp.json()
    except ValueError:
        return None
    value = data.get(name) if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


class GatewayLogin:
    """The assistant as an interactive OAuth client of the gateway (DCR, PKCE, code grant)."""

    def __init__(
        self,
        gateway_url: str,
        public_url: str,
        store: MemoryStore,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        pending_ttl_s: float = 600,
    ) -> None:
        self._gateway = gateway_url.rstrip("/")
        self._redirect_uri = f"{public_url.rstrip('/')}/oauth/callback"
        self._store = store
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient()
        self._clock = clock
        self._pending_ttl_s = pending_ttl_s
        self._pending: dict[str, _Pending] = {}

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    async def ensure_registered(self) -> str:
        """The gateway client id, registered (RFC 7591) on first use and persisted."""
        existing = self._store.get_preference(CLIENT_ID_KEY)
        if existing:
            return existing
        body = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [self._redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        try:
            resp = await self._http.post(f"{self._gateway}/register", json=body)
        except httpx.HTTPError as exc:
            raise LoginError(f"gateway unreachable: {exc}") from exc
        if resp.status_code != 201:
            raise LoginError(
                f"registration refused ({resp.status_code}): {resp.text[:_ERROR_BODY_CHARS]}"
            )
        client_id = _json_field(resp, "client_id")
        if not client_id:
            raise LoginError("registration reply has no client_id")
        self._store.set_preference(CLIENT_ID_KEY, client_id)
        log.info("registered with the gateway as %s", client_id)
        return client_id

    def _client_id(self) -> str:
        client_id = self._store.get_preference(CLIENT_ID_KEY)
        if not client_id:
            raise LoginError("not registered with the gateway")
        return client_id

    def _purge_expired(self) -> None:
        cutoff = self._clock() - self._pending_ttl_s
        for state in [s for s, p in self._pending.items() if p.created_at < cutoff]:
            del self._pending[state]
        while len(self._pending) >= MAX_PENDING_LOGINS:  # dicts keep insertion order
            del self._pending[next(iter(self._pending))]

    def start(self) -> tuple[str, str]:
        """``(authorize_url, state)``; the PKCE verifier stays here, keyed by ``state``."""
        client_id = self._client_id()
        self._purge_expired()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        self._pending[state] = _Pending(client_id, verifier, created_at=self._clock())
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self._redirect_uri,
            "code_challenge": _s256(verifier),
            "code_challenge_method": "S256",
            "state": state,
            "resource": f"{self._gateway}/mcp",
        }
        return f"{self._gateway}/authorize?{urlencode(params)}", state

    async def finish(self, code: str, state: str) -> bool:
        """Exchange ``code`` for the pending ``state`` (single use); True if a token came back."""
        pending = self._pending.pop(state, None)
        if pending is None or self._clock() - pending.created_at > self._pending_ttl_s:
            log.warning("login callback with an unknown or expired state")
            return False
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": pending.client_id,
            "code_verifier": pending.verifier,
            "resource": f"{self._gateway}/mcp",
        }
        try:
            resp = await self._http.post(f"{self._gateway}/token", data=form)
        except httpx.HTTPError as exc:
            log.warning("login code exchange failed: %s", exc)
            return False
        ok = resp.is_success and bool(_json_field(resp, "access_token"))
        if not ok:
            log.warning("login code exchange refused (%s)", resp.status_code)
        return ok
