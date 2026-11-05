"""Tests for ``pdl-assistant-briefing``: offline retry, delivery, exit codes."""

from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pdl_assistant import cli_briefing
from pdl_assistant.cli_briefing import run_briefing
from pdl_assistant.config import AssistantConfig
from pdl_assistant.memory import MemoryStore
from pdl_assistant.notify import NotifyError

CONFIG = AssistantConfig(
    gateway_url="http://gateway.test",
    ollama_url="http://ollama.test",
    model="m",
    client_secret_file="/x",
    secrets_file="/y",
    db_path="/z",
    public_url="http://assistant.test",
)
# 14:30 UTC on 4 Nov 2026 is 08:30 in Chicago (CST, UTC-6).
START = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


class FakeLlm:
    def __init__(self, online_at_probe: int | None) -> None:
        self.online_at_probe = online_at_probe
        self.probes = 0

    async def probe(self) -> bool:
        self.probes += 1
        return self.online_at_probe is not None and self.probes >= self.online_at_probe


class FakeGateway:
    async def list_tools(self) -> list:
        return []


class FakeNotifier:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.error = error

    async def send(self, title: str, body: str) -> None:
        if self.error:
            raise self.error
        self.sent.append((title, body))


class FakeClock:
    """Advances by the slept seconds, like real time."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def memory(tmp_path: Path):
    store = MemoryStore(tmp_path / "a.db")
    yield store
    store.close()


def _row_count(memory) -> int:
    return memory._read("SELECT COUNT(*) AS n FROM briefings")[0]["n"]


async def _run(memory, llm, notifier, start=START, retry=True, config=CONFIG):
    clock = FakeClock(start)
    status = await run_briefing(
        config,
        now=clock.now,
        sleep=clock.sleep,
        notifier=notifier,
        llm=llm,
        gateway=FakeGateway(),
        memory=memory,
        retry=retry,
    )
    return status, clock


async def test_online_sends_once(memory):
    llm, notifier = FakeLlm(1), FakeNotifier()
    status, clock = await _run(memory, llm, notifier)
    assert status == "sent"
    assert clock.sleeps == []
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == "Morning briefing"
    last = memory.last_briefing()
    assert last.status == "sent"
    assert last.text == notifier.sent[0][1]


async def test_offline_then_online_at_second_probe(memory):
    llm, notifier = FakeLlm(2), FakeNotifier()
    status, clock = await _run(memory, llm, notifier)
    assert status == "sent"
    assert clock.sleeps == [900]
    assert llm.probes == 2
    assert len(notifier.sent) == 1
    assert _row_count(memory) == 1


async def test_offline_past_cutoff_is_skipped(memory):
    llm, notifier = FakeLlm(None), FakeNotifier()
    status, clock = await _run(memory, llm, notifier)
    assert status == "skipped"
    assert notifier.sent == []
    assert sum(clock.sleeps) >= 90 * 60  # 08:30 -> 10:00 local
    last = memory.last_briefing()
    assert (last.status, last.note) == ("skipped", "brain offline")


async def test_cutoff_uses_local_time(memory):
    # 15:50 UTC is 09:50 Chicago: one more sleep; 16:05 UTC is 10:05 Chicago: stop at once.
    status, clock = await _run(
        memory,
        FakeLlm(None),
        FakeNotifier(),
        start=datetime(2026, 11, 4, 15, 50, tzinfo=UTC),
    )
    assert (status, clock.sleeps) == ("skipped", [900])
    status, clock = await _run(
        memory,
        FakeLlm(None),
        FakeNotifier(),
        start=datetime(2026, 11, 4, 16, 5, tzinfo=UTC),
    )
    assert (status, clock.sleeps) == ("skipped", [])


async def test_send_failure_records_failed_with_text(memory):
    notifier = FakeNotifier(NotifyError("ntfy returned HTTP 500"))
    status, _ = await _run(memory, FakeLlm(1), notifier)
    assert status == "failed"
    last = memory.last_briefing()
    assert last.status == "failed"
    assert "HTTP 500" in last.note
    assert last.text.startswith("Morning briefing")


async def test_build_failure_records_failed(memory, monkeypatch):
    async def boom(self):
        raise RuntimeError("kaput")

    monkeypatch.setattr(cli_briefing.BriefingBuilder, "build", boom)
    status, _ = await _run(memory, FakeLlm(1), FakeNotifier())
    assert status == "failed"
    assert memory.last_briefing().note == "kaput"


async def test_now_offline_probes_once_and_records_nothing(memory):
    llm, notifier = FakeLlm(None), FakeNotifier()
    status, clock = await _run(memory, llm, notifier, retry=False)
    assert status == "offline"
    assert (llm.probes, clock.sleeps, notifier.sent) == (1, [], [])
    assert memory.last_briefing() is None


async def test_now_online_sends(memory):
    status, _ = await _run(memory, FakeLlm(1), FakeNotifier(), retry=False)
    assert status == "sent"


async def test_custom_cutoff(memory):
    config = replace(CONFIG, briefing_retry_until="08:30")
    status, clock = await _run(memory, FakeLlm(None), FakeNotifier(), config=config)
    assert (status, clock.sleeps) == ("skipped", [])


# --- main() -----------------------------------------------------------------------------------


def _config_file(tmp_path: Path) -> Path:
    (tmp_path / "client.secret").write_text("s\n", encoding="utf-8")
    path = tmp_path / "a.yaml"
    path.write_text(
        "\n".join(
            [
                "gateway_url: http://gateway.test",
                "ollama_url: http://ollama.test",
                "model: m",
                f"client_secret_file: {(tmp_path / 'client.secret').as_posix()}",
                "secrets_file: /nope",
                f"db_path: {(tmp_path / 'a.db').as_posix()}",
                "public_url: http://assistant.test",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ASSISTANT_CONFIG", "ASSISTANT_SECRETS", "ASSISTANT_NTFY_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(("error", "code"), [(None, 0), (NotifyError("down"), 1)])
def test_test_notification(tmp_path, monkeypatch, error, code):
    notifier = FakeNotifier(error)
    monkeypatch.setattr(cli_briefing, "notifier_from_config", lambda config: notifier)
    assert (
        cli_briefing.main(["--config", str(_config_file(tmp_path)), "--test-notification"]) == code
    )
    if not error:
        assert notifier.sent == [("Test", "Test from the assistant")]
    assert not (tmp_path / "a.db").exists()  # memory untouched


@pytest.mark.parametrize(
    ("status", "code"), [("sent", 0), ("skipped", 0), ("failed", 1), ("offline", 2)]
)
def test_main_exit_codes(tmp_path, monkeypatch, status, code):
    async def fake_run(config, **kwargs):
        assert kwargs["retry"] is (status != "offline")
        return status

    monkeypatch.setattr(cli_briefing, "run_briefing", fake_run)
    args = ["--config", str(_config_file(tmp_path)), *(["--now"] if status == "offline" else [])]
    assert cli_briefing.main(args) == code
    assert (tmp_path / "a.db").exists()


def test_main_bad_config_is_1(tmp_path, capsys):
    assert cli_briefing.main(["--config", str(tmp_path / "missing.yaml")]) == 1
    assert "pdl-assistant-briefing" in capsys.readouterr().err


async def test_skip_then_success_opens_with_skipped_prefix(memory):
    from pdl_assistant.briefing import SKIPPED_PREFIX

    assert (await _run(memory, FakeLlm(None), FakeNotifier()))[0] == "skipped"
    notifier = FakeNotifier()
    assert (await _run(memory, FakeLlm(1), notifier))[0] == "sent"
    assert notifier.sent[0][1].startswith(SKIPPED_PREFIX)
    assert _row_count(memory) == 2


async def test_raising_aclose_is_logged_and_memory_still_closes(monkeypatch, caplog):
    closed = []

    class Memory:
        def close(self) -> None:
            closed.append(True)

    class Client:
        async def aclose(self) -> None:
            raise RuntimeError("close boom")

    async def fake_run(config, **kwargs):
        return "sent"

    monkeypatch.setattr(cli_briefing, "build_clients", lambda c, s: (Memory(), Client(), Client()))
    monkeypatch.setattr(cli_briefing, "read_client_secret", lambda path: "s")
    monkeypatch.setattr(cli_briefing, "run_briefing", fake_run)
    args = Namespace(now=False)
    with caplog.at_level("ERROR"):
        assert await cli_briefing._run(args, CONFIG, FakeNotifier()) == 0
    assert closed == [True]
    assert caplog.text.count("error while closing a client") == 2


async def test_flags_section_reads_stored_flags_without_opening_checks_file(memory):
    missing = "/nonexistent/flagger.yaml"  # never opened: the switch is only "is it set"
    config = replace(CONFIG, flagger_config_file=missing)
    memory.add_flag("big", "t1", "big: -600 SHOP", START.isoformat())
    notifier = FakeNotifier()
    status, _ = await _run(memory, FakeLlm(1), notifier, config=config)
    assert status == "sent"
    assert "Flags: big: -600 SHOP" in notifier.sent[0][1]


async def test_flags_section_not_configured_without_flagger_file(memory):
    notifier = FakeNotifier()
    await _run(memory, FakeLlm(1), notifier)
    assert "Flags: no checks configured yet." in notifier.sent[0][1]
