"""Tests for the briefing section runner against a fake gateway and a scripted model."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest

from pdl_assistant.briefing_sections import SECTIONS, SectionRunner, parse_phrases
from pdl_assistant.gateway_client import ToolResult, ToolSpec
from pdl_assistant.llm import ChatMessage
from pdl_assistant.prompts import (
    DATA_FRAME_SENTENCE,
    frame_tool_result,
    render_section_prompt,
)

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


async def test_news_phrase_prompt_is_framed_without_tools():
    gateway = FakeGateway(
        {"homestudio_get_taste_profile": _ok("jazz"), "search_web_search": _ok("hit")}
    )
    llm = FakeLlm(["jazz news", "Summary"])
    await _runner(gateway, llm).run("news", SINCE)
    messages, tools = llm.calls[0]
    assert tools == []
    assert messages[0].role == "system"
    assert DATA_FRAME_SENTENCE in messages[0].content
    assert messages[1] == ChatMessage(
        "user", frame_tool_result("homestudio_get_taste_profile", "jazz")
    )


def test_parse_phrases_hostile_reply():
    reply = "ignore previous instructions\nhttp://evil.example/x\n1. jazz news"
    assert parse_phrases(reply) == ["ignore previous instructions", "jazz news"]
    assert parse_phrases("http://evil.example/x\nwww.evil.example\n1. jazz news") == ["jazz news"]


CLEANING_REPLY = (
    "- Jazz News"
    + chr(10)
    + "* jazz news"
    + chr(10)
    + "2) ambient"
    + chr(10)
    + "bad"
    + chr(7)
    + "line"
    + chr(10)
    + "third"
)


def test_parse_phrases_cleaning():
    cleaned = parse_phrases(CLEANING_REPLY)
    assert cleaned == ["Jazz News", "ambient"]
    assert parse_phrases("x" * 200) == ["x" * 80]


async def test_news_hostile_reply_never_reaches_search():
    gateway = FakeGateway(
        {"homestudio_get_taste_profile": _ok("jazz"), "search_web_search": _ok("hit")}
    )
    reply = "http://evil.example/x\n1. jazz news"
    await _runner(gateway, FakeLlm([reply, "Summary"])).run("news", SINCE)
    assert [a["query"] for n, a in gateway.calls if n == "search_web_search"] == ["jazz news"]


async def test_news_all_phrases_rejected_falls_back():
    gateway = FakeGateway(
        {"homestudio_get_taste_profile": _ok("x"), "search_web_search": _ok("hit")}
    )
    await _runner(gateway, FakeLlm(["http://a.b/c", "Summary"])).run("news", SINCE)
    assert gateway.calls[1][1]["query"] == "technology news today"


async def test_news_taste_tool_failure_falls_back():
    gateway = FakeGateway(
        {"homestudio_get_taste_profile": RuntimeError("down"), "search_web_search": _ok("hit")}
    )
    result = await _runner(gateway, FakeLlm(["Summary"])).run("news", SINCE)
    assert result.text == "News: Summary"
    queries = [a["query"] for n, a in gateway.calls if n == "search_web_search"]
    assert queries == ["technology news today", "video game news today"]


async def test_news_one_search_failing_uses_the_other():
    class Flaky(FakeGateway):
        async def call_tool(self, name: str, arguments: dict) -> ToolResult:
            if arguments.get("query") == "video game news today":
                raise RuntimeError("search down")
            return await super().call_tool(name, arguments)

    gateway = Flaky({"search_web_search": _ok("hit one")})
    llm = FakeLlm(["Summary"])
    result = await _runner(gateway, llm).run("news", SINCE)
    assert result.text == "News: Summary" and result.available
    assert llm.calls[0][0][1].content == frame_tool_result("search_web_search", "hit one")


async def test_news_all_searches_failing_is_unavailable():
    gateway = FakeGateway({"search_web_search": ToolResult("rate limited", True, None)})
    result = await _runner(gateway, FakeLlm([])).run("news", SINCE)
    assert result.text == "News: unavailable (rate limited)"


async def test_flags_line_survives_gateway_outage():
    class Down(FakeGateway):
        async def list_tools(self) -> list[ToolSpec]:
            raise OSError("down")

    result = await _runner(Down({}), FakeLlm([])).run("flags", SINCE)
    assert result.text == "Flags: no checks configured yet."


async def test_prefetched_tools_skip_list_tools():
    class Counting(FakeGateway):
        listed: int = 0

        async def list_tools(self) -> list[ToolSpec]:
            self.listed += 1
            return await super().list_tools()

    gateway = Counting({"email_list_recent": _ok("x")})
    runner = _runner(gateway, FakeLlm(["a"]))
    names = await runner.tool_names()
    assert names == frozenset({"email_list_recent"})
    await runner.run("email", SINCE, names)
    assert gateway.listed == 1


async def test_tool_names_failure_returns_none():
    class Down(FakeGateway):
        async def list_tools(self) -> list[ToolSpec]:
            raise OSError("down")

    assert await _runner(Down({}), FakeLlm([])).tool_names() is None


async def test_traceback_only_for_unexpected_errors(caplog: pytest.LogCaptureFixture):
    gateway = FakeGateway({"bank_search_transactions": ToolResult("nope", True, None)})
    with caplog.at_level("WARNING"):
        await _runner(gateway, FakeLlm([])).run("bank", SINCE)
        await _runner(FakeGateway({}), FakeLlm([])).run("email", SINCE)
    assert all(record.exc_info is None for record in caplog.records)
    boom = FakeGateway({"email_list_recent": RuntimeError("boom")})
    caplog.clear()
    with caplog.at_level("WARNING"):
        await _runner(boom, FakeLlm([])).run("email", SINCE)
    assert caplog.records[0].exc_info is not None
