"""OAuth 2.1 authorization-server provider for the MCP SDK, backed by ``TokenStore``.

Interactive clients register dynamically and are approved by a human with the approval
passphrase; headless grants (e.g. ``personal-assistant``) use only ``client_credentials``.
Tokens are opaque random strings, persisted hashed and revocable.
"""

import logging
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from pdl_gateway.config import GatewayConfig, GatewaySecrets, GrantConfig
from pdl_gateway.secrets import hash_secret, new_secret, verify_secret
from pdl_gateway.store import PendingApproval, TokenStore

logger = logging.getLogger(__name__)

_TOKEN_BYTES = 32


class ApprovalError(Exception):
    """The approval passphrase was wrong."""


@dataclass(frozen=True)
class ApprovalView:
    """What the approval page shows for a pending request."""

    request_id: str
    client_name: str
    redirect_uri: str
    scopes: tuple[str, ...]


@cache
def _dummy_hash() -> str:
    """A hash to verify against for unknown clients, so timing does not reveal them."""
    return hash_secret(new_secret())


class GatewayAuthProvider:
    """Implements the SDK's ``OAuthAuthorizationServerProvider`` protocol."""

    def __init__(
        self,
        config: GatewayConfig,
        secrets: GatewaySecrets,
        store: TokenStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._secrets = secrets
        self._store = store
        self._clock = clock

    # --- clients ---------------------------------------------------------

    def _headless_grant(self, client_id: str | None) -> GrantConfig | None:
        grant = self.config.grant(client_id) if client_id else None
        return grant if grant is not None and grant.kind == "headless" else None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        grant = self._headless_grant(client_id)
        if grant is None:
            return self._store.get_client(client_id)
        return OAuthClientInformationFull(
            client_id=grant.name,
            token_endpoint_auth_method="none",
            grant_types=["client_credentials"],
            redirect_uris=None,
            scope=" ".join(grant.scopes),
            client_name=grant.name,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._store.save_client(client_info)
        name = (client_info.client_name or "")[:80]
        logger.info("registered client %s (%r)", client_info.client_id, name)

    def _client_name(self, client_id: str) -> str:
        if self._headless_grant(client_id) is not None:
            return client_id
        client = self._store.get_client(client_id)
        return (client.client_name if client else None) or client_id

    # --- authorization + human approval ------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if client.client_id is None or self._headless_grant(client.client_id) is not None:
            raise AuthorizeError("unauthorized_client", "this client cannot use /authorize")
        request_id = secrets.token_urlsafe(_TOKEN_BYTES)
        self._store.save_pending(
            PendingApproval(
                request_id=request_id,
                client_id=client.client_id,
                client_name=client.client_name,
                params_json=params.model_dump_json(),
                expires_at=self._clock() + self.config.auth_code_ttl_s,
            )
        )
        return f"{self.config.issuer_url.rstrip('/')}/approve?request={request_id}"

    def approval_view(self, request_id: str) -> ApprovalView | None:
        """Describe a live pending request without consuming it."""
        pending = self._store.get_pending(request_id, self._clock())
        if pending is None:
            return None
        params = AuthorizationParams.model_validate_json(pending.params_json)
        return ApprovalView(
            request_id=request_id,
            client_name=pending.client_name or pending.client_id,
            redirect_uri=str(params.redirect_uri),
            scopes=self.config.interactive_scopes,
        )

    def complete_approval(self, request_id: str, passphrase: str, approved: bool) -> str:
        """Return the client redirect URL; the passphrase is verified before any code is issued.

        A cheap lookup runs first so unknown requests never cost a PBKDF2 derivation.
        """
        if self._store.get_pending(request_id, self._clock()) is None:
            raise LookupError("unknown or expired approval request")
        if not verify_secret(passphrase, self._secrets.approval_passphrase_hash):
            logger.warning("approval %s: wrong passphrase", request_id[:8])
            raise ApprovalError("wrong passphrase")
        now = self._clock()
        pending = self._store.pop_pending(request_id, now)
        if pending is None:  # expired or consumed while the passphrase was being checked
            raise LookupError("unknown or expired approval request")
        params = AuthorizationParams.model_validate_json(pending.params_json)
        redirect = str(params.redirect_uri)
        if not approved:
            logger.info("approval denied for client %s", pending.client_id)
            return construct_redirect_uri(redirect, error="access_denied", state=params.state)
        code = AuthorizationCode(
            code=secrets.token_urlsafe(_TOKEN_BYTES),
            scopes=list(self.config.interactive_scopes),
            expires_at=now + self.config.auth_code_ttl_s,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.config.resource_url,
        )
        self._store.save_code(code)
        logger.info("approval granted for client %s", pending.client_id)
        return construct_redirect_uri(redirect, code=code.code, state=params.state)

    # --- token minting ---------------------------------------------------

    def _issue_tokens(
        self,
        client_id: str,
        scopes: Sequence[str],
        *,
        with_refresh: bool,
        refresh_expires_at: int | None,
    ) -> OAuthToken:
        now = int(self._clock())
        access = secrets.token_urlsafe(_TOKEN_BYTES)
        ttl = self.config.access_token_ttl_s
        self._store.save_token("access", access, client_id, scopes, now + ttl)
        refresh = None
        if with_refresh:
            refresh = secrets.token_urlsafe(_TOKEN_BYTES)
            expires = (
                refresh_expires_at
                if refresh_expires_at is not None
                else now + self.config.refresh_token_ttl_s
            )
            self._store.save_token("refresh", refresh, client_id, scopes, expires)
        return OAuthToken(
            access_token=access, expires_in=ttl, scope=" ".join(scopes), refresh_token=refresh
        )

    # --- authorization code grant -----------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._store.get_code(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._store.delete_code(authorization_code.code) == 0:  # single use
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue_tokens(
            authorization_code.client_id,
            authorization_code.scopes,
            with_refresh=True,
            refresh_expires_at=None,
        )

    # --- refresh token grant ---------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = self._store.load_token("refresh", refresh_token, int(self._clock()))
        if stored is None or stored.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=stored.client_id,
            scopes=list(stored.scopes),
            expires_at=stored.expires_at,
            resource=self.config.resource_url,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate; ``scopes`` (request-derived) is ignored so a session can never narrow for good."""
        if self._store.revoke(refresh_token.token) == 0:
            raise TokenError("invalid_grant", "refresh token already used or revoked")
        return self._issue_tokens(
            refresh_token.client_id,
            refresh_token.scopes,
            with_refresh=True,
            refresh_expires_at=refresh_token.expires_at,
        )

    # --- client credentials grant ------------------------------------------

    def issue_client_credentials(self, client_id: str, client_secret: str) -> OAuthToken:
        """Mint an access token for a headless grant; same error for unknown or wrong."""
        grant = self._headless_grant(client_id)
        encoded = self._secrets.client_secret_hashes.get(client_id) if grant else None
        valid = verify_secret(client_secret, encoded or _dummy_hash())
        if grant is None or encoded is None or not valid:
            logger.warning("client_credentials rejected for %r", client_id[:64])
            raise TokenError("invalid_client", "unknown client or wrong secret")
        return self._issue_tokens(
            grant.name, grant.scopes, with_refresh=False, refresh_expires_at=None
        )

    # --- resource server + revocation ------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = self._store.load_token("access", token, int(self._clock()))
        if stored is None:
            return None
        return AccessToken(
            token=token,
            client_id=stored.client_id,
            scopes=list(stored.scopes),
            expires_at=stored.expires_at,
            resource=self.config.resource_url,
            claims={"client_name": self._client_name(stored.client_id)},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke every live token of the client: an access/refresh pair is not linked."""
        count = self._store.revoke_client_tokens(token.client_id)
        logger.info("revoked %d token(s) for client %s", count, token.client_id)
