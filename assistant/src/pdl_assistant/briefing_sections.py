"""Morning briefing sections: gather data through the gateway, summarise with the model."""

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from .gateway_client import ToolResult, ToolSpec
from .llm import ChatMessage
from .prompts import frame_tool_result, render_phrase_prompt, render_section_prompt

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
PHRASE_MAX_CHARS = 80
PHRASE_LIMIT = 2
_BULLET = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")
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


def _clean_phrase(line: str) -> str | None:
    phrase = _BULLET.sub("", line).strip()
    lowered = phrase.lower()
    if not phrase or "://" in phrase or "www." in lowered:
        return None
    if any(not ch.isprintable() for ch in phrase):
        return None
    return phrase[:PHRASE_MAX_CHARS].strip()


def parse_phrases(text: str, limit: int = PHRASE_LIMIT) -> list[str]:
    """Safe search phrases from model output: no bullets, URLs or control characters."""
    phrases: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        phrase = _clean_phrase(line)
        if phrase and phrase.lower() not in seen:
            seen.add(phrase.lower())
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

    async def tool_names(self) -> frozenset[str] | None:
        """Names of the gateway's tools, or None when the gateway cannot be asked."""
        try:
            return frozenset(spec.name for spec in await self._gateway.list_tools())
        except Exception as exc:
            logger.warning("briefing could not list gateway tools: %s", exc, exc_info=True)
            return None

    async def run(
        self, name: str, since: datetime, tools: frozenset[str] | None = None
    ) -> SectionResult:
        title = TITLES.get(name, name.title())
        try:
            return await self._dispatch(name, since, tools)
        except _Unavailable as exc:
            logger.warning("briefing section %s unavailable: %s", name, exc)
            return SectionResult(name, f"{title}: unavailable ({_reason(exc)})", False)
        except Exception as exc:
            logger.warning("briefing section %s failed: %s", name, exc, exc_info=True)
            return SectionResult(name, f"{title}: unavailable ({_reason(exc)})", False)

    async def _dispatch(
        self, name: str, since: datetime, tools: frozenset[str] | None
    ) -> SectionResult:
        if name not in TITLES:
            raise _Unavailable("unknown section")
        if name == "flags":
            return SectionResult(name, NOT_CONNECTED[name], False)
        if tools is None:
            tools = frozenset(spec.name for spec in await self._gateway.list_tools())
        if name == "texts" and SMS_TOOL not in tools:
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

    async def _news(self, tools: frozenset[str]) -> SectionResult:
        if SEARCH_TOOL not in tools:
            raise _Unavailable(f"tool {SEARCH_TOOL} is not available")
        phrases = await self._news_phrases(TASTE_TOOL in tools)
        hits: list[str] = []
        failure: Exception | None = None
        for phrase in phrases:
            args = {"query": phrase, "recency": "day", "limit": 3}
            try:
                hits.append((await self._call(SEARCH_TOOL, args)).text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("briefing news search failed: %s", exc)
                failure = failure or exc
        if not hits and failure is not None:
            raise failure
        instruction = INSTRUCTIONS["news"].format(count=self._news_count)
        summary = await self._summarise("news", instruction, SEARCH_TOOL, "\n\n".join(hits))
        return SectionResult("news", summary, True)

    async def _news_phrases(self, has_taste: bool) -> list[str]:
        if not has_taste:
            return list(FALLBACK_PHRASES)
        try:
            taste = await self._call(TASTE_TOOL, {})
            data = frame_tool_result(TASTE_TOOL, taste.text)
            answer = await self._ask(render_phrase_prompt(), data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("briefing taste step failed, using fixed phrases: %s", exc)
            return list(FALLBACK_PHRASES)
        return parse_phrases(answer) or list(FALLBACK_PHRASES)
