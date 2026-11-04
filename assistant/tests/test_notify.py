"""Tests for the ntfy notifier and its config wiring."""

import logging
from pathlib import Path

import httpx
import pytest
import respx

from pdl_assistant.config import AssistantConfig, ConfigError, load_config
from pdl_assistant.notify import (
    LogNotifier,
    NotifyError,
    NtfyNotifier,
    notifier_from_config,
)

NTFY = "https://ntfy.example"
ENV = {
    "ASSISTANT_GATEWAY_URL": "https://gateway.example",
    "ASSISTANT_OLLAMA_URL": "http://ollama.example:11434",
    "ASSISTANT_MODEL": "m",
    "ASSISTANT_CLIENT_SECRET_FILE": "/run/cs",
    "ASSISTANT_SECRETS_FILE": "/run/s.yaml",
    "ASSISTANT_DB_PATH": "/data/a.db",
    "ASSISTANT_PUBLIC_URL": "https://assistant.example",
}


def _config(**extra: str) -> AssistantConfig:
    return load_config(None, {**ENV, **extra})


@respx.mock
async def test_posts_text_with_headers_and_bearer():
    route = respx.post(f"{NTFY}/briefing").respond(200)
    await NtfyNotifier(NTFY, "briefing", "tk_synthetic").send("Morning", "hello")
    req = route.calls.last.request
    assert req.content == b"hello"
    assert req.headers["Title"] == "Morning"
    assert req.headers["Markdown"] == "no"
    assert req.headers["Authorization"] == "Bearer tk_synthetic"


@respx.mock
async def test_no_authorization_without_token_and_trailing_slash():
    route = respx.post(f"{NTFY}/t").respond(200)
    await NtfyNotifier(NTFY + "/", "t", None).send("T", "b")
    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
async def test_non_ascii_title_is_sent_as_utf8_without_newlines():
    route = respx.post(f"{NTFY}/t").respond(200)
    await NtfyNotifier(NTFY, "t", None).send("Café\nbrief", "b")
    raw = dict(route.calls.last.request.headers.raw)
    assert raw[b"Title"] == "Café brief".encode()


@respx.mock
async def test_unauthorized_raises_notify_error():
    respx.post(f"{NTFY}/briefing").respond(401, text="nope")
    with pytest.raises(NotifyError, match="401"):
        await NtfyNotifier(NTFY, "briefing", "bad").send("T", "b")


@respx.mock
async def test_connection_error_raises_notify_error():
    respx.post(f"{NTFY}/briefing").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(NotifyError):
        await NtfyNotifier(NTFY, "briefing", None).send("T", "b")


@respx.mock
async def test_title_and_body_are_capped_at_char_boundary():
    route = respx.post(f"{NTFY}/briefing").respond(200)
    notifier = NtfyNotifier(NTFY, "briefing", None)
    await notifier.send("x" * 300, "é" * 3000)  # 6000 bytes; 4000 is a boundary
    req = route.calls.last.request
    assert len(req.headers["Title"]) == 120
    assert req.content.decode("utf-8") == "é" * 2000

    await notifier.send("T", "a" + "é" * 3000)  # byte 4000 splits a character
    sent = route.calls.last.request.content
    assert len(sent) == 3999
    sent.decode("utf-8")  # must not raise


@respx.mock
async def test_injected_client_is_used_and_not_closed():
    route = respx.post(f"{NTFY}/briefing").respond(200)
    async with httpx.AsyncClient() as client:
        await NtfyNotifier(NTFY, "briefing", None, http=client).send("T", "b")
        assert not client.is_closed
    assert route.called


async def test_log_notifier_logs_title_and_length_not_body(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger="pdl_assistant.notify"):
        await LogNotifier().send("Morning", "secret body text")
    assert "Morning" in caplog.text
    assert "16" in caplog.text
    assert "secret body text" not in caplog.text


def test_from_config_without_url_is_log_notifier():
    assert isinstance(notifier_from_config(_config()), LogNotifier)


@respx.mock
async def test_from_config_with_url_and_token_file(tmp_path: Path):
    token = tmp_path / "tok"
    token.write_text("tk_synthetic\n", encoding="utf-8")
    cfg = _config(
        ASSISTANT_NTFY_URL=NTFY,
        ASSISTANT_NTFY_TOPIC="alerts",
        ASSISTANT_NTFY_TOKEN_FILE=str(token),
    )
    notifier = notifier_from_config(cfg)
    assert isinstance(notifier, NtfyNotifier)
    route = respx.post(f"{NTFY}/alerts").respond(200)
    await notifier.send("T", "b")
    assert route.calls.last.request.headers["Authorization"] == "Bearer tk_synthetic"


def test_from_config_url_without_token_file():
    assert isinstance(notifier_from_config(_config(ASSISTANT_NTFY_URL=NTFY)), NtfyNotifier)


def test_from_config_missing_token_file_is_config_error(tmp_path: Path):
    cfg = _config(ASSISTANT_NTFY_URL=NTFY, ASSISTANT_NTFY_TOKEN_FILE=str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="ntfy_token_file"):
        notifier_from_config(cfg)


def test_config_defaults_and_url_validation():
    cfg = _config()
    assert (cfg.ntfy_url, cfg.ntfy_topic, cfg.ntfy_token_file) == (None, "briefing", None)
    with pytest.raises(ConfigError, match="ntfy_url"):
        _config(ASSISTANT_NTFY_URL="ntfy.example")
