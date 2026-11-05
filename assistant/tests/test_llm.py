"""Tests for the Ollama client: model probe, chat with tool calls, offline detection."""

import json

import httpx
import pytest
import respx

from pdl_assistant.llm import (
    OFFLINE_TEXT,
    ChatMessage,
    LlmError,
    OfflineError,
    OllamaClient,
    ToolCall,
)

BASE = "http://ollama.test:11434"
MODEL = "qwen3:14b"
TOOL = {
    "type": "function",
    "function": {"name": "homestudio_get_taste_profile", "description": "", "parameters": {}},
}


def _client(model: str = MODEL) -> OllamaClient:
    return OllamaClient(BASE, model, timeout_s=5)


def _tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [{"name": n} for n in names]})


def _reply(message: dict) -> httpx.Response:
    return httpx.Response(200, json={"model": MODEL, "message": message, "done": True})


@respx.mock
async def test_probe_true_when_model_listed() -> None:
    respx.get(f"{BASE}/api/tags").mock(return_value=_tags("llama3:latest", MODEL))
    assert await _client().probe() is True
    assert await _client("llama3").probe() is True  # ":latest" is implied


@respx.mock
async def test_probe_false_when_model_missing() -> None:
    respx.get(f"{BASE}/api/tags").mock(return_value=_tags("llama3:latest"))
    assert await _client().probe() is False
    respx.get(f"{BASE}/api/tags").mock(return_value=httpx.Response(200, text="not json"))
    assert await _client().probe() is False


@respx.mock
async def test_probe_false_on_connection_error() -> None:
    respx.get(f"{BASE}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    assert await _client().probe() is False
    respx.get(f"{BASE}/api/tags").mock(side_effect=httpx.ReadTimeout("slow"))
    assert await _client().probe() is False


@respx.mock
async def test_chat_parses_tool_calls() -> None:
    calls = [
        {"function": {"name": "homestudio_get_taste_profile", "arguments": {}}},
        {
            "function": {
                "name": "homestudio_get_listening_history",
                "arguments": json.dumps({"limit": 5}),
            }
        },
    ]
    respx.post(f"{BASE}/api/chat").mock(
        return_value=_reply({"role": "assistant", "content": "", "tool_calls": calls})
    )

    reply = await _client().chat([ChatMessage("user", "what do I like?")], [TOOL])

    assert reply == ChatMessage(
        "assistant",
        "",
        tool_calls=(
            ToolCall("homestudio_get_taste_profile", {}),
            ToolCall("homestudio_get_listening_history", {"limit": 5}),
        ),
    )


@respx.mock
async def test_chat_plain_answer() -> None:
    respx.post(f"{BASE}/api/chat").mock(
        return_value=_reply({"role": "assistant", "content": "Jazz, mostly."})
    )
    reply = await _client().chat([ChatMessage("user", "hi")], [])
    assert reply == ChatMessage("assistant", "Jazz, mostly.")


@respx.mock
async def test_chat_offline_error_on_5xx_and_timeout() -> None:
    route = respx.post(f"{BASE}/api/chat")
    route.mock(return_value=httpx.Response(503, text="busy"))
    with pytest.raises(OfflineError):
        await _client().chat([ChatMessage("user", "hi")], [])
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OfflineError):
        await _client().chat([ChatMessage("user", "hi")], [])
    route.mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(OfflineError):
        await _client().chat([ChatMessage("user", "hi")], [])
    route.mock(return_value=httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(LlmError, match="model not found"):
        await _client().chat([ChatMessage("user", "hi")], [])
    assert OFFLINE_TEXT.startswith("Brain is offline")


@respx.mock
async def test_chat_sends_tools_and_num_ctx() -> None:
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=_reply({"role": "assistant", "content": "ok"})
    )
    history = [
        ChatMessage("system", "be brief"),
        ChatMessage("user", "taste?"),
        ChatMessage(
            "assistant", "", tool_calls=(ToolCall("homestudio_get_taste_profile", {"a": 1}),)
        ),
        ChatMessage("tool", '{"genres": ["jazz"]}', tool_name="homestudio_get_taste_profile"),
    ]

    await OllamaClient(BASE, MODEL, num_ctx=8192).chat(history, [TOOL])

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "taste?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "homestudio_get_taste_profile", "arguments": {"a": 1}}}
                ],
            },
            {
                "role": "tool",
                "content": '{"genres": ["jazz"]}',
                "tool_name": "homestudio_get_taste_profile",
            },
        ],
        "tools": [TOOL],
        "stream": False,
        "options": {"num_ctx": 8192},
    }


@respx.mock
@pytest.mark.parametrize("think", [True, False])
async def test_chat_sends_think_only_when_configured(think: bool) -> None:
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=_reply({"role": "assistant", "content": "ok"})
    )

    await OllamaClient(BASE, MODEL, think=think).chat([ChatMessage("user", "hi")], [])

    assert json.loads(route.calls.last.request.content)["think"] is think


@respx.mock
async def test_chat_omits_think_by_default() -> None:
    route = respx.post(f"{BASE}/api/chat").mock(
        return_value=_reply({"role": "assistant", "content": "ok"})
    )

    await OllamaClient(BASE, MODEL).chat([ChatMessage("user", "hi")], [])

    assert "think" not in json.loads(route.calls.last.request.content)


async def test_injected_client_is_used() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _tags(MODEL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OllamaClient(BASE, MODEL, http=http)
        assert await client.probe() is True
        await client.aclose()  # does not close an injected client
        assert not http.is_closed


def test_offline_text_is_exact() -> None:
    assert OFFLINE_TEXT == "Brain is offline (workstation asleep or GPU busy). Try again later."


@respx.mock
async def test_malformed_tool_arguments_is_llm_error() -> None:
    route = respx.post(f"{BASE}/api/chat")
    for arguments in ("{not json", "[1, 2]"):
        call = {"function": {"name": "homestudio_get_taste_profile", "arguments": arguments}}
        route.mock(return_value=_reply({"role": "assistant", "content": "", "tool_calls": [call]}))
        with pytest.raises(LlmError):
            await _client().chat([ChatMessage("user", "hi")], [])


@respx.mock
async def test_probe_never_raises_on_unexpected_error() -> None:
    respx.get(f"{BASE}/api/tags").mock(side_effect=RuntimeError("boom"))
    assert await _client().probe() is False


async def test_probe_is_bounded_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    from pdl_assistant import llm

    monkeypatch.setattr(llm, "_PROBE_TIMEOUT_S", 0.05)

    async def handler(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(5)
        return _tags(MODEL)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with anyio.fail_after(2):
            assert await OllamaClient(BASE, MODEL, http=http).probe() is False
