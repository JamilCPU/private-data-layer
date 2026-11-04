"""The agent loop: model turns, gateway tool calls framed as data, memory of every step."""

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from .gateway_client import GatewayClient, GatewayUnavailable, ToolSpec, to_ollama
from .llm import OFFLINE_TEXT, ChatMessage, LlmError, OfflineError, OllamaClient, ToolCall
from .memory import MemoryStore, StoredMessage
from .prompts import frame_tool_result, render_system_prompt, safe_tool_name

logger = logging.getLogger(__name__)

STEP_CAP_NOTE = "\n\n(I stopped after the tool-step limit.)"
LLM_ERROR_TEXT = "The model returned something I could not read; please try again."
TOOLS_UNAVAILABLE_NOTE = "Tools are unavailable right now."

_HISTORY_ROLES = frozenset({"user", "assistant", "tool"})


@dataclass(frozen=True)
class Reply:
    text: str
    tool_calls_made: tuple[str, ...]
    offline: bool
    step_capped: bool


def _from_stored(row: StoredMessage) -> ChatMessage:
    if row.role == "tool":
        return ChatMessage("tool", row.content, tool_name=row.tool_name)
    return ChatMessage(row.role, row.content)  # type: ignore[arg-type]


def _calls_json(calls: Sequence[ToolCall]) -> str:
    """The tool calls of an assistant turn as JSON, so stored history replays coherently."""
    return json.dumps([{"name": c.name, "arguments": c.arguments} for c in calls])


class Agent:
    """Answers one user message at a time using the model and the gateway's tools."""

    def __init__(
        self,
        llm: OllamaClient,
        gateway: GatewayClient,
        memory: MemoryStore,
        *,
        max_tool_steps: int = 6,
        history_limit: int = 20,
        tool_cache_ttl_s: float = 60.0,
        system_prompt_extra: str = "",
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = lambda: date.today(),
    ) -> None:
        self._llm = llm
        self._gateway = gateway
        self._memory = memory
        self._max_steps = max_tool_steps
        self._history_limit = history_limit
        self._cache_ttl_s = tool_cache_ttl_s
        self._extra = system_prompt_extra
        self._clock = clock
        self._today = today
        self._specs: tuple[ToolSpec, ...] | None = None
        self._specs_expire_at = 0.0

    async def reply(self, conversation_id: int, user_text: str) -> Reply:
        """Store ``user_text``, run the model/tool loop, store and return the answer."""
        self._memory.add_message(conversation_id, "user", user_text)
        if not await self._llm.probe():
            reply = Reply(OFFLINE_TEXT, (), offline=True, step_capped=False)
        else:
            specs, note = await self._tool_specs()
            messages = [self._system_message(note), *self._history(conversation_id)]
            reply = await self._loop(conversation_id, messages, specs)
        self._memory.add_message(conversation_id, "assistant", reply.text)
        return reply

    async def _tool_specs(self) -> tuple[tuple[ToolSpec, ...], str]:
        """The gateway's tool specs (cached) and a system-prompt note when none are available."""
        now = self._clock()
        if self._specs is not None and now < self._specs_expire_at:
            return self._specs, ""
        try:
            specs = tuple(await self._gateway.list_tools())
        except GatewayUnavailable as exc:
            logger.warning("tools unavailable, answering without them: %s", exc)
            return (), TOOLS_UNAVAILABLE_NOTE
        self._specs, self._specs_expire_at = specs, now + self._cache_ttl_s
        return specs, ""

    def _system_message(self, note: str) -> ChatMessage:
        extra = "\n".join(part for part in (self._extra, note) if part)
        return ChatMessage("system", render_system_prompt(self._today(), extra))

    def _history(self, conversation_id: int) -> list[ChatMessage]:
        """The last stored messages, oldest first (already including the new user message)."""
        rows = self._memory.recent_messages(conversation_id, limit=self._history_limit)
        return [_from_stored(row) for row in rows if row.role in _HISTORY_ROLES]

    async def _loop(
        self, conversation_id: int, messages: list[ChatMessage], specs: tuple[ToolSpec, ...]
    ) -> Reply:
        tools = [to_ollama(spec) for spec in specs]
        known = frozenset(spec.name for spec in specs)
        made: tuple[str, ...] = ()
        last_text = ""
        for _ in range(self._max_steps):
            try:
                answer = await self._llm.chat(messages, tools)
            except OfflineError:
                return Reply(OFFLINE_TEXT, made, offline=True, step_capped=False)
            except LlmError as exc:
                logger.warning("unreadable model reply: %s", exc)
                return Reply(LLM_ERROR_TEXT, made, offline=False, step_capped=False)
            if not answer.tool_calls:
                return Reply(answer.content, made, offline=False, step_capped=False)
            last_text = answer.content
            self._memory.add_message(conversation_id, "assistant", _calls_json(answer.tool_calls))
            results, called = await self._run_calls(conversation_id, answer.tool_calls, known)
            messages = [*messages, answer, *results]
            made = (*made, *called)
        return Reply(last_text + STEP_CAP_NOTE, made, offline=False, step_capped=True)

    async def _run_calls(
        self, conversation_id: int, calls: Sequence[ToolCall], known: frozenset[str]
    ) -> tuple[list[ChatMessage], tuple[str, ...]]:
        """Run each call; return the framed tool messages and the names sent to the gateway."""
        results: list[ChatMessage] = []
        called: list[str] = []
        for call in calls:
            if call.name in known:
                called.append(call.name)
                text = await self._call_gateway(call)
            else:
                logger.warning("model asked for an unknown tool: %r", call.name)
                text = f"unknown tool: {call.name}"
            name = safe_tool_name(call.name)
            content = frame_tool_result(name, text)
            self._memory.add_message(conversation_id, "tool", content, tool_name=name)
            results.append(ChatMessage("tool", content, tool_name=name))
        return results, tuple(called)

    async def _call_gateway(self, call: ToolCall) -> str:
        """The tool's text (an ``is_error`` result passes its text through)."""
        try:
            result = await self._gateway.call_tool(call.name, call.arguments)
        except GatewayUnavailable as exc:
            return f"tool unavailable: {exc}"
        return result.text
