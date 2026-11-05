"""Ollama chat client with tool calls and offline detection."""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import anyio
import httpx

logger = logging.getLogger(__name__)

OFFLINE_TEXT = "Brain is offline (workstation asleep or GPU busy). Try again later."

_PROBE_TIMEOUT_S = 3.0

Role = Literal["system", "user", "assistant", "tool"]


class LlmError(Exception):
    """Ollama rejected the request (4xx) or sent a reply that could not be understood."""


class OfflineError(Exception):
    """Ollama could not be reached, timed out, or failed on its side (5xx)."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_name: str | None = None


def _to_wire(message: ChatMessage) -> dict[str, Any]:
    """A message in Ollama's /api/chat format."""
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {"function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    if message.tool_name is not None:
        wire["tool_name"] = message.tool_name
    return wire


def _parse_call(raw: Any) -> ToolCall:
    function = raw["function"]
    arguments = function.get("arguments") or {}
    if isinstance(arguments, str):
        arguments = json.loads(arguments) if arguments.strip() else {}
    if not isinstance(arguments, dict):
        raise TypeError(f"tool arguments must be an object, got {type(arguments).__name__}")
    return ToolCall(name=str(function["name"]), arguments=arguments)


def _from_wire(body: Any) -> ChatMessage:
    """The assistant message of an /api/chat reply; LlmError when it is malformed."""
    try:
        message = body["message"]
        calls = tuple(_parse_call(raw) for raw in message.get("tool_calls") or ())
        return ChatMessage("assistant", str(message.get("content") or ""), tool_calls=calls)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise LlmError(f"malformed reply from Ollama: {exc}") from exc


def _error_detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error") or response.text)
    except (ValueError, AttributeError):
        return response.text or f"HTTP {response.status_code}"


def _model_listed(model: str, names: Sequence[str]) -> bool:
    bare = model.removesuffix(":latest")
    return any(name == model or name.removesuffix(":latest") == bare for name in names)


class OllamaClient:
    """Chat with a model served by Ollama."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 120.0,
        num_ctx: int = 16384,
        think: bool | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self.model = model
        self._timeout_s = timeout_s
        self._num_ctx = num_ctx
        self._think = think  # None leaves the model's own default in place
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient()

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    async def probe(self) -> bool:
        """True when Ollama answers within 3 s and serves the configured model; never raises."""
        try:
            with anyio.fail_after(_PROBE_TIMEOUT_S):
                response = await self._http.get(f"{self._base}/api/tags", timeout=_PROBE_TIMEOUT_S)
                response.raise_for_status()
                names = [str(m["name"]) for m in response.json()["models"]]
        except Exception as exc:  # noqa: BLE001 - the probe must never raise
            logger.info("ollama probe failed: %s", exc or type(exc).__name__)
            return False
        return _model_listed(self.model, names)

    async def chat(self, messages: Sequence[ChatMessage], tools: Sequence[dict]) -> ChatMessage:
        """One non-streaming chat turn; OfflineError when Ollama is unreachable or failing."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_wire(m) for m in messages],
            "tools": list(tools),
            "stream": False,
            "options": {"num_ctx": self._num_ctx},
        }
        if self._think is not None:
            payload["think"] = self._think
        try:
            response = await self._http.post(
                f"{self._base}/api/chat", json=payload, timeout=self._timeout_s
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("ollama offline: %s", exc or type(exc).__name__)
            raise OfflineError(str(exc) or type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise OfflineError(str(exc) or type(exc).__name__) from exc
        if response.status_code >= 500:
            logger.warning("ollama answered %d", response.status_code)
            raise OfflineError(f"Ollama answered {response.status_code}")
        if response.status_code >= 400:
            raise LlmError(_error_detail(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise LlmError("Ollama sent a non-JSON reply") from exc
        return _from_wire(body)
