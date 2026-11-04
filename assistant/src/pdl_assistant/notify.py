"""Push notifications: a self-hosted ntfy server, or a logging stand-in for dev and tests."""

import logging
from pathlib import Path
from typing import Protocol

import httpx

from pdl_assistant.config import AssistantConfig, ConfigError

log = logging.getLogger(__name__)

_MAX_TITLE_CHARS = 120
_MAX_BODY_BYTES = 4000


class NotifyError(Exception):
    """Raised when a notification could not be delivered."""


class Notifier(Protocol):
    async def send(self, title: str, body: str) -> None: ...


def _cap_title(title: str) -> str:
    return " ".join(title.split())[:_MAX_TITLE_CHARS]


def _cap_body(body: str) -> bytes:
    """Cut to the byte limit without splitting a UTF-8 character."""
    cut = body.encode("utf-8")[:_MAX_BODY_BYTES].decode("utf-8", errors="ignore")
    return cut.encode("utf-8")


class NtfyNotifier:
    def __init__(
        self,
        url: str,
        topic: str,
        token: str | None,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self._endpoint = f"{url.rstrip('/')}/{topic}"
        self._token = token
        self._http = http
        self._timeout_s = timeout_s

    def _headers(self, title: str) -> dict[str, bytes | str]:
        # UTF-8 bytes: httpx rejects a non-ASCII str header; ntfy reads UTF-8 headers.
        headers: dict[str, bytes | str] = {
            "Title": _cap_title(title).encode("utf-8"),
            "Markdown": "no",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _post(self, client: httpx.AsyncClient, title: str, body: str) -> httpx.Response:
        return await client.post(
            self._endpoint,
            content=_cap_body(body),
            headers=self._headers(title),
            timeout=self._timeout_s,
        )

    async def send(self, title: str, body: str) -> None:
        try:
            if self._http is not None:
                response = await self._post(self._http, title, body)
            else:
                async with httpx.AsyncClient() as client:
                    response = await self._post(client, title, body)
        except httpx.HTTPError as exc:
            raise NotifyError(f"ntfy request failed: {exc}") from exc
        if not response.is_success:
            raise NotifyError(f"ntfy returned HTTP {response.status_code}")


class LogNotifier:
    async def send(self, title: str, body: str) -> None:
        log.info("notification %r (%d chars)", title, len(body))


def notifier_from_config(config: AssistantConfig) -> Notifier:
    """ntfy when ``ntfy_url`` is set, otherwise a notifier that only logs."""
    if config.ntfy_url is None:
        return LogNotifier()
    token: str | None = None
    if config.ntfy_token_file is not None:
        path = config.ntfy_token_file
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"cannot read ntfy_token_file {path}: {exc}") from exc
    return NtfyNotifier(config.ntfy_url, config.ntfy_topic, token or None)
