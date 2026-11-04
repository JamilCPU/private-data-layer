"""Tests for the briefing section runner against a fake gateway and a scripted model."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest

from pdl_assistant.briefing_sections import SECTIONS, SectionRunner
from pdl_assistant.gateway_client import ToolResult, ToolSpec
from pdl_assistant.llm import ChatMessage
from pdl_assistant.prompts import frame_tool_result, render_section_prompt

SINCE = datetime(2026, 11, 3, 7, 30, tzinfo=UTC)
TODAY = date(2026, 11, 4)
EMAIL_INSTRUCTION = (
    "Mention senders and subjects that look like they need a reply or a payment; skip newsletters."
)


def _ok(text: str) -> ToolResult:
    return ToolResult(text, False, None)


@dataclass
class FakeGateway:
    results: dict[str, ToolResult | Exception]
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name, "", {}) for name in self.results]

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, arguments))
        result = self.results[name]
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeLlm:
    replies: list[str | Exception]
    calls: list[tuple[list[ChatMessage], list[dict]]] = field(default_factory=list)

    async def chat(self, messages: Sequence[ChatMessage], tools: Sequence[dict]) -> ChatMessage:
        self.calls.append((list(messages), list(tools)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ChatMessage("assistant", reply)


def _runner(gateway: FakeGateway, llm: FakeLlm, **kwargs) -> SectionRunner:
    return SectionRunner(gateway, llm, today=lambda: TODAY, **kwargs)  # type: ignore[arg-type]


def test_sections_order():
    assert SECTIONS == ("email", "texts", "bank", "news", "flags")


async def test_email_plan_and_framing():
    gateway = FakeGateway({"email_list_recent": _ok("From a: invoice")})
    llm = FakeLlm(["  Pay invoice from a  "])
    result = await _runner(gateway, llm).run("email", SINCE)
    assert gateway.calls == [("email_list_recent", {"since": SINCE.isoformat(), "limit": 20})]
    assert result.available
    assert result.text == "Email: Pay invoice from a"
    messages, tools = llm.calls[0]
    assert tools == []
    system = render_section_prompt("Email", EMAIL_INSTRUCTION, TODAY)
    assert messages[0] == ChatMessage("system", system)
    framed = frame_tool_result("email_list_recent", "From a: invoice")
    assert messages[1] == ChatMessage("user", framed)


async def test_nothing_new_reply():
    gateway = FakeGateway({"email_list_recent": _ok("")})
    result = await _runner(gateway, FakeLlm(["Nothing new."])).run("email", SINCE)
    assert result.text == "Email: nothing new"
    assert result.available


async def test_bank_plan_uses_date():
    gateway = FakeGateway({"bank_search_transactions": _ok("-60 shop")})
    result = await _runner(gateway, FakeLlm(["Shop 60"])).run("bank", SINCE)
    assert gateway.calls == [("bank_search_transactions", {"since": "2026-11-03", "limit": 50})]
    assert result.text == "Bank: Shop 60"


async def test_texts_missing_tool():
    result = await _runner(FakeGateway({}), FakeLlm([])).run("texts", SINCE)
    assert (result.text, result.available) == ("Texts: not connected yet.", False)


async def test_texts_present():
    gateway = FakeGateway({"sms_list_recent_sms": _ok("hi")})
    result = await _runner(gateway, FakeLlm(["A text"])).run("texts", SINCE)
    assert gateway.calls[0][0] == "sms_list_recent_sms"
    assert result.text == "Texts: A text"


async def test_flags_missing():
    result = await _runner(FakeGateway({}), FakeLlm([])).run("flags", SINCE)
    assert (result.text, result.available) == ("Flags: no checks configured yet.", False)


async def test_news_phrases_split_and_deduped():
    gateway = FakeGateway(
        {"homestudio_get_taste_profile": _ok("jazz"), "search_web_search": _ok("hit")}
    )
    llm = FakeLlm(["jazz news\n\n  jazz news \nambient gigs\nthird", "Headline - site.com"])
    result = await _runner(gateway, llm, news_count=2).run("news", SINCE)
    searches = [
        ("search_web_search", {"query": "jazz news", "recency": "day", "limit": 3}),
        ("search_web_search", {"query": "ambient gigs", "recency": "day", "limit": 3}),
    ]
    assert gateway.calls[1:] == searches
    assert result.text == "News: Headline - site.com"
    final_system, final_user = llm.calls[1][0]
    assert final_user.content == frame_tool_result("search_web_search", "hit\n\nhit")
    assert "2 most relevant" in final_system.content


async def test_news_without_taste_tool_uses_fixed_phrases():
    gateway = FakeGateway({"search_web_search": _ok("hit")})
    result = await _runner(gateway, FakeLlm(["Summary"])).run("news", SINCE)
    queries = [args["query"] for _, args in gateway.calls]
    assert queries == ["technology news today", "video game news today"]
    assert result.text == "News: Summary"


async def test_news_without_search_tool():
    gateway = FakeGateway({"homestudio_get_taste_profile": _ok("jazz")})
    result = await _runner(gateway, FakeLlm([])).run("news", SINCE)
    assert result.available is False
    assert result.text.startswith("News: unavailable (")


async def test_news_blank_phrases_fall_back():
    gateway = FakeGateway(
        {"homestudio_get_taste_profile": _ok("x"), "search_web_search": _ok("hit")}
    )
    await _runner(gateway, FakeLlm(["  \n", "Summary"])).run("news", SINCE)
    assert gateway.calls[1][1]["query"] == "technology news today"


async def test_tool_exception_is_unavailable(caplog: pytest.LogCaptureFixture):
    gateway = FakeGateway({"email_list_recent": RuntimeError("boom\nsecond line")})
    with caplog.at_level("WARNING"):
        result = await _runner(gateway, FakeLlm([])).run("email", SINCE)
    assert (result.text, result.available) == ("Email: unavailable (boom)", False)
    assert "email" in caplog.text


async def test_tool_error_result_is_unavailable():
    gateway = FakeGateway({"bank_search_transactions": ToolResult("no access", True, None)})
    result = await _runner(gateway, FakeLlm([])).run("bank", SINCE)
    assert result.text == "Bank: unavailable (no access)"


async def test_model_error_is_unavailable_and_reason_capped():
    gateway = FakeGateway({"email_list_recent": _ok("x")})
    result = await _runner(gateway, FakeLlm([RuntimeError("y" * 300)])).run("email", SINCE)
    assert result.available is False
    assert result.text == "Email: unavailable (" + "y" * 120 + ")"


async def test_empty_exception_message_uses_type_name():
    gateway = FakeGateway({"email_list_recent": TimeoutError()})
    result = await _runner(gateway, FakeLlm([])).run("email", SINCE)
    assert result.text == "Email: unavailable (TimeoutError)"


async def test_list_tools_failure_is_unavailable():
    class Broken(FakeGateway):
        async def list_tools(self) -> list[ToolSpec]:
            raise OSError("down")

    result = await _runner(Broken({}), FakeLlm([])).run("email", SINCE)
    assert result.text == "Email: unavailable (down)"


async def test_unknown_section_is_unavailable():
    result = await _runner(FakeGateway({}), FakeLlm([])).run("weather", SINCE)
    assert result.available is False
