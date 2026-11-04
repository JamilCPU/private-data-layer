"""Morning briefing sections: gather data through the gateway, summarise with the model."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from .gateway_client import ToolResult, ToolSpec
from .llm import ChatMessage
from .prompts import frame_tool_result, render_section_prompt

logger = logging.getLogger(__name__)

SECTIONS = ("email", "texts", "bank", "news", "flags")
TITLES = {
    "email": "Email",
    "texts": "Texts",
    "bank": "Bank",
    "news": "News",
    "flags": "Flags",
}

EMAIL_TOOL = "email_list_recent"
SMS_TOOL = "sms_list_recent_sms"
BANK_TOOL = "bank_search_transactions"
TASTE_TOOL = "homestudio_get_taste_profile"
SEARCH_TOOL = "search_web_search"
NOTHING_NEW = "nothing new"
REASON_MAX_CHARS = 120
FALLBACK_PHRASES = ("technology news today", "video game news today")
PHRASE_INSTRUCTION = (
    "From this taste profile write two short web search phrases for today's news, "
    "one per line, nothing else."
)
INSTRUCTIONS = {
    "email": "Mention senders and subjects that look like they need a reply or a payment; "
    "skip newsletters.",
    "texts": "Mention texts that need a reply; skip automated messages.",
    "bank": "List new charges over 50 and any income; total the spending.",
    "news": "Pick the {count} most relevant items; one line each: headline, source domain.",
}
NOT_CONNECTED = {
    "texts": "Texts: not connected yet.",
    "flags": "Flags: no checks configured yet.",
}


class _Gateway(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class _Llm(Protocol):
    async def chat(self, messages: Sequence[ChatMessage], tools: Sequence[dict]) -> ChatMessage: ...


@dataclass(frozen=True)
class SectionResult:
    name: str
    text: str
    available: bool


class _Unavailable(Exception):
    """A section cannot be produced; the message is the reason shown to the owner."""


def _reason(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    first = lines[0].strip() if lines else ""
    return (first or type(exc).__name__)[:REASON_MAX_CHARS]


def parse_phrases(text: str, limit: int = 2) -> list[str]:
    """Distinct non-empty lines of ``text``, at most ``limit``."""
    phrases: list[str] = []
    for line in text.splitlines():
        phrase = line.strip()
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return phrases[:limit]


class SectionRunner:
    """Runs one briefing section; ``run`` never raises."""

    def __init__(
        self,
        gateway: _Gateway,
        llm: _Llm,
        *,
        today: Callable[[], date],
        news_count: int = 3,
        tool_timeout_s: float = 60,
    ) -> None:
        self._gateway = gateway
        self._llm = llm
        self._today = today
        self._news_count = news_count
        self._timeout = tool_timeout_s

    async def run(self, name: str, since: datetime) -> SectionResult:
        title = TITLES.get(name, name.title())
        try:
            return await self._dispatch(name, since)
        except Exception as exc:
            logger.warning("briefing section %s unavailable: %s", name, exc, exc_info=True)
            return SectionResult(name, f"{title}: unavailable ({_reason(exc)})", False)

    async def _dispatch(self, name: str, since: datetime) -> SectionResult:
        if name not in TITLES:
            raise _Unavailable("unknown section")
        tools = {spec.name for spec in await self._gateway.list_tools()}
        if name == "flags" or (name == "texts" and SMS_TOOL not in tools):
            return SectionResult(name, NOT_CONNECTED[name], False)
        if name == "news":
            return await self._news(tools)
        tool, args = {
            "email": (EMAIL_TOOL, {"since": since.isoformat(), "limit": 20}),
            "texts": (SMS_TOOL, {"since": since.isoformat(), "limit": 20}),
            "bank": (BANK_TOOL, {"since": since.date().isoformat(), "limit": 50}),
        }[name]
        if tool not in tools:
            raise _Unavailable(f"tool {tool} is not available")
        result = await self._call(tool, args)
        summary = await self._summarise(name, INSTRUCTIONS[name], tool, result.text)
        return SectionResult(name, summary, True)

    async def _call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        result = await asyncio.wait_for(
            self._gateway.call_tool(tool, arguments), timeout=self._timeout
        )
        if result.is_error:
            raise _Unavailable(result.text.strip() or f"{tool} failed")
        return result

    async def _ask(self, system: str, data: str) -> str:
        reply = await self._llm.chat([ChatMessage("system", system), ChatMessage("user", data)], [])
        return reply.content.strip()

    async def _summarise(self, name: str, instruction: str, tool: str, text: str) -> str:
        title = TITLES[name]
        system = render_section_prompt(title, instruction, self._today())
        answer = await self._ask(system, frame_tool_result(tool, text))
        if not answer:
            raise _Unavailable("empty model reply")
        if answer.rstrip(".! ").lower() == NOTHING_NEW:
            answer = NOTHING_NEW
        return f"{title}: {answer}"

    async def _news(self, tools: set[str]) -> SectionResult:
        if SEARCH_TOOL not in tools:
            raise _Unavailable(f"tool {SEARCH_TOOL} is not available")
        phrases = await self._news_phrases(TASTE_TOOL in tools)
        hits = []
        for phrase in phrases:
            args = {"query": phrase, "recency": "day", "limit": 3}
            hits.append((await self._call(SEARCH_TOOL, args)).text)
        instruction = INSTRUCTIONS["news"].format(count=self._news_count)
        summary = await self._summarise("news", instruction, SEARCH_TOOL, "\n\n".join(hits))
        return SectionResult("news", summary, True)

    async def _news_phrases(self, has_taste: bool) -> list[str]:
        if not has_taste:
            return list(FALLBACK_PHRASES)
        taste = await self._call(TASTE_TOOL, {})
        answer = await self._ask(PHRASE_INSTRUCTION, frame_tool_result(TASTE_TOOL, taste.text))
        return parse_phrases(answer) or list(FALLBACK_PHRASES)
