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
STEP_CAP_FALLBACK = "I could not finish within the tool-step limit."
LLM_ERROR_TEXT = "The model returned something I could not read; please try again."
FAILURE_TEXT = "Something went wrong; please try again."
TOOLS_UNAVAILABLE_NOTE = "Tools are unavailable right now."
SKIPPED_TEXT = "tool call skipped: too many calls in one turn"
MAX_CALLS_PER_TURN = 8
CALLS_MARK = "__calls__"  # tool_name of the stored assistant row that records a turn's calls

_NOT_REPLAYED = (OFFLINE_TEXT, LLM_ERROR_TEXT, FAILURE_TEXT)


@dataclass(frozen=True)
class Reply:
    text: str
    tool_calls_made: tuple[str, ...]
    offline: bool
    step_capped: bool


@dataclass(frozen=True)
class _CallOutcome:
    text: str
    error: bool
    sent: bool  # the call reached the gateway


def _from_stored(row: StoredMessage) -> ChatMessage:
    return ChatMessage(row.role, row.content)  # type: ignore[arg-type]


def _calls_json(calls: Sequence[ToolCall]) -> str:
    """The tool calls of an assistant turn as JSON, stored for the UI (never replayed)."""
    return json.dumps([{"name": c.name, "arguments": c.arguments} for c in calls])


def _capped(text: str, made: tuple[str, ...]) -> Reply:
    body = text.strip() or STEP_CAP_FALLBACK
    return Reply(body + STEP_CAP_NOTE, made, offline=False, step_capped=True)


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
        try:
            reply = await self._answer(conversation_id)
        except Exception:  # the user always gets an answer; logger.exception keeps the cause
            logger.exception("reply failed in conversation %d", conversation_id)
            reply = Reply(FAILURE_TEXT, (), offline=False, step_capped=False)
        self._memory.add_message(conversation_id, "assistant", reply.text)
        return reply

    async def _answer(self, conversation_id: int) -> Reply:
        if not await self._llm.probe():
            return Reply(OFFLINE_TEXT, (), offline=True, step_capped=False)
        specs, note = await self._tool_specs()
        messages = [self._system_message(note), *self._history(conversation_id)]
        return await self._loop(conversation_id, messages, specs)

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
        """The last user and final assistant texts, oldest first (including the new message)."""
        rows = self._memory.recent_turns(conversation_id, self._history_limit, _NOT_REPLAYED)
        return [_from_stored(row) for row in rows]

    async def _loop(
        self, conversation_id: int, messages: list[ChatMessage], specs: tuple[ToolSpec, ...]
    ) -> Reply:
        """Up to ``max_tool_steps`` model turns; the last one gets no tools and must answer."""
        tools = [to_ollama(spec) for spec in specs]
        known = frozenset(spec.name for spec in specs)
        made: tuple[str, ...] = ()
        for step in range(1, self._max_steps + 1):
            final = step == self._max_steps
            try:
                answer = await self._llm.chat(messages, [] if final else tools)
            except OfflineError:
                return Reply(OFFLINE_TEXT, made, offline=True, step_capped=False)
            except LlmError as exc:
                logger.warning("unreadable model reply: %s", exc)
                return Reply(LLM_ERROR_TEXT, made, offline=False, step_capped=False)
            if final and (answer.tool_calls or step > 1):
                return _capped(answer.content, made)
            if not answer.tool_calls:
                return Reply(answer.content, made, offline=False, step_capped=False)
            calls_row = _calls_json(answer.tool_calls)
            self._memory.add_message(conversation_id, "assistant", calls_row, tool_name=CALLS_MARK)
            results, called = await self._run_calls(conversation_id, answer.tool_calls, known)
            messages = [*messages, answer, *results]
            made = (*made, *called)
        raise AssertionError("unreachable: max_tool_steps must be >= 1")  # pragma: no cover

    async def _run_calls(
        self, conversation_id: int, calls: Sequence[ToolCall], known: frozenset[str]
    ) -> tuple[list[ChatMessage], tuple[str, ...]]:
        """Run each call; return the framed tool messages and the names sent to the gateway."""
        results: list[ChatMessage] = []
        called: list[str] = []
        for index, call in enumerate(calls):
            outcome = await self._outcome(call, known, over_cap=index >= MAX_CALLS_PER_TURN)
            if outcome.sent:
                called.append(call.name)
            name = safe_tool_name(call.name)
            content = frame_tool_result(name, outcome.text, error=outcome.error)
            self._memory.add_message(conversation_id, "tool", content, tool_name=name)
            results.append(ChatMessage("tool", content, tool_name=name))
        return results, tuple(called)

    async def _outcome(
        self, call: ToolCall, known: frozenset[str], *, over_cap: bool
    ) -> _CallOutcome:
        if over_cap:
            return _CallOutcome(SKIPPED_TEXT, error=True, sent=False)
        if call.name not in known:
            logger.warning("model asked for an unknown tool: %r", call.name)
            return _CallOutcome(f"unknown tool: {call.name}", error=True, sent=False)
        try:
            result = await self._gateway.call_tool(call.name, call.arguments)
        except GatewayUnavailable as exc:
            return _CallOutcome(f"tool unavailable: {exc}", error=True, sent=True)
        return _CallOutcome(result.text, error=result.is_error, sent=True)
