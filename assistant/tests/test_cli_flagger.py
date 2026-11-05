"""Tests for ``pdl-assistant-flagger``: exit codes, the INFO line, client handling."""

import logging
from pathlib import Path

import pytest

from pdl_assistant import cli_flagger
from pdl_assistant.flagger import FlaggerOutcome
from pdl_assistant.gateway_client import ToolResult
from pdl_assistant.memory import MemoryStore

CHECKS_YAML = "checks:\n  - name: big\n    type: transaction_over\n    amount: 500\n"


class FakeGateway:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.closed = False

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        if not self.ok:
            return ToolResult("boom", True, None)
        row = {
            "id": "t1",
            "posted_on": "2026-11-01",
            "amount": "-600.00",
            "description": "SHOP",
            "category": None,
            "account": "Everyday",
        }
        return ToolResult("ok", False, {"result": [row]})

    async def aclose(self) -> None:
        self.closed = True


class FakeLlm:
    def __init__(self) -> None:
        self.probes = 0
        self.closed = False

    async def probe(self) -> bool:
        self.probes += 1
        return False

    async def aclose(self) -> None:
        self.closed = True


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, title: str, body: str) -> None:
        self.sent.append((title, body))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ASSISTANT_CONFIG", "ASSISTANT_SECRETS", "ASSISTANT_NTFY_URL"):
        monkeypatch.delenv(name, raising=False)


def _config(tmp_path: Path, *, checks: bool = True, extra: tuple[str, ...] = ()) -> Path:
    (tmp_path / "client.secret").write_text("s\n", encoding="utf-8")
    (tmp_path / "flagger.yaml").write_text(CHECKS_YAML, encoding="utf-8")
    lines = [
        "gateway_url: http://gateway.test",
        "ollama_url: http://ollama.test",
        "model: m",
        f"client_secret_file: {(tmp_path / 'client.secret').as_posix()}",
        "secrets_file: /nope",
        f"db_path: {(tmp_path / 'a.db').as_posix()}",
        "public_url: http://assistant.test",
        *extra,
    ]
    if checks:
        lines.append(f"flagger_config_file: {(tmp_path / 'flagger.yaml').as_posix()}")
    path = tmp_path / "a.yaml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _wire(monkeypatch, tmp_path, gateway, llm, notifier):
    def build(config, secret):
        return MemoryStore(config.db_path), gateway, llm

    monkeypatch.setattr(cli_flagger, "build_clients", build)
    monkeypatch.setattr(cli_flagger, "notifier_from_config", lambda config: notifier)


def test_notified_exit_0_and_info_line(tmp_path, monkeypatch, caplog):
    gateway, llm, notifier = FakeGateway(), FakeLlm(), FakeNotifier()
    _wire(monkeypatch, tmp_path, gateway, llm, notifier)
    with caplog.at_level(logging.INFO):
        assert cli_flagger.main(["--config", str(_config(tmp_path))]) == 0
    assert "flagger notified new=1 notified=1 failed_checks=-" in caplog.text
    assert "SHOP" not in caplog.text
    assert len(notifier.sent) == 1
    assert llm.probes == 0  # no model call unless flagger_require_brain
    assert gateway.closed and llm.closed


def test_require_brain_offline_skips_with_exit_0(tmp_path, monkeypatch, caplog):
    gateway, llm, notifier = FakeGateway(), FakeLlm(), FakeNotifier()
    _wire(monkeypatch, tmp_path, gateway, llm, notifier)
    config = _config(tmp_path, extra=("flagger_require_brain: true",))
    with caplog.at_level(logging.INFO):
        assert cli_flagger.main(["--config", str(config)]) == 0
    assert llm.probes == 1
    assert notifier.sent == []
    assert "flagger skipped_offline new=0 notified=0 failed_checks=-" in caplog.text


def test_all_checks_failing_exit_1(tmp_path, monkeypatch, caplog):
    _wire(monkeypatch, tmp_path, FakeGateway(ok=False), FakeLlm(), FakeNotifier())
    with caplog.at_level(logging.INFO):
        assert cli_flagger.main(["--config", str(_config(tmp_path))]) == 1
    assert "failed_checks=big" in caplog.text


class PartlyFailingGateway(FakeGateway):
    """Transactions work, the recurring tool errors: one of two checks fails."""

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        if name == "bank_recurring_charges":
            return ToolResult("boom", True, None)
        return await super().call_tool(name, arguments)


def test_some_checks_failing_exit_0_and_logged(tmp_path, monkeypatch, caplog):
    notifier = FakeNotifier()
    _wire(monkeypatch, tmp_path, PartlyFailingGateway(), FakeLlm(), notifier)
    config = _config(tmp_path)
    (tmp_path / "flagger.yaml").write_text(
        CHECKS_YAML + "  - name: sub\n    type: recurring_change\n    percent: 20\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO):
        assert cli_flagger.main(["--config", str(config)]) == 0
    assert "failed_checks=sub" in caplog.text
    assert "check sub failed: " in caplog.text
    assert len(notifier.sent) == 1


def test_all_checks_failing_still_sends_older_pending_flags(tmp_path, monkeypatch, caplog):
    notifier = FakeNotifier()
    _wire(monkeypatch, tmp_path, FakeGateway(ok=False), FakeLlm(), notifier)
    config = _config(tmp_path)
    store = MemoryStore(tmp_path / "a.db")
    store.add_flag("big", "old", "big: -700 EARLIER", "2026-11-01T00:00:00+00:00")
    store.close()
    with caplog.at_level(logging.INFO):
        code = cli_flagger.main(["--config", str(config)])
    assert code == 1  # the plan: exit 1 when every check failed, even though the send worked
    assert notifier.sent == [("Flagged: big", "big: -700 EARLIER")]
    assert "flagger notified new=0 notified=1 failed_checks=big" in caplog.text
    store = MemoryStore(tmp_path / "a.db")
    assert store.unnotified_flags() == []
    store.close()


def test_notify_failed_exit_1(tmp_path, monkeypatch):
    async def fake_run(*args, **kwargs):
        return FlaggerOutcome(1, 0, (), "notify_failed")

    _wire(monkeypatch, tmp_path, FakeGateway(), FakeLlm(), FakeNotifier())
    monkeypatch.setattr(cli_flagger, "run_flagger", fake_run)
    assert cli_flagger.main(["--config", str(_config(tmp_path))]) == 1


def test_dry_run_sends_nothing(tmp_path, monkeypatch):
    notifier = FakeNotifier()
    _wire(monkeypatch, tmp_path, FakeGateway(), FakeLlm(), notifier)
    assert cli_flagger.main(["--config", str(_config(tmp_path)), "--dry-run"]) == 0
    assert notifier.sent == []
    store = MemoryStore(tmp_path / "a.db")
    assert store.unnotified_flags() == []
    store.close()


def test_flagger_config_file_unset_is_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_flagger, "notifier_from_config", lambda config: FakeNotifier())
    assert cli_flagger.main(["--config", str(_config(tmp_path, checks=False))]) == 1
    err = capsys.readouterr().err
    assert err.startswith("pdl-assistant-flagger:")
    assert "flagger_config_file is not set" in err


def test_invalid_checks_file_is_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_flagger, "notifier_from_config", lambda config: FakeNotifier())
    config = _config(tmp_path)
    (tmp_path / "flagger.yaml").write_text("checks: []\n", encoding="utf-8")
    assert cli_flagger.main(["--config", str(config)]) == 1
    assert capsys.readouterr().err.startswith("pdl-assistant-flagger:")


def test_missing_checks_file_is_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_flagger, "notifier_from_config", lambda config: FakeNotifier())
    config = _config(tmp_path)
    (tmp_path / "flagger.yaml").unlink()
    assert cli_flagger.main(["--config", str(config)]) == 1
    assert capsys.readouterr().err.startswith("pdl-assistant-flagger:")


async def test_raising_aclose_is_logged_and_memory_closes(tmp_path, monkeypatch, caplog):
    closed = []

    class Memory:
        def close(self) -> None:
            closed.append(True)

    class Client:
        async def aclose(self) -> None:
            raise RuntimeError("close boom")

    async def fake_run(*args, **kwargs):
        return FlaggerOutcome(0, 0, (), "quiet")

    from argparse import Namespace

    from pdl_assistant.config import load_config

    config = load_config(_config(tmp_path), {})
    monkeypatch.setattr(cli_flagger, "build_clients", lambda c, s: (Memory(), Client(), Client()))
    monkeypatch.setattr(cli_flagger, "run_flagger", fake_run)
    with caplog.at_level("ERROR"):
        assert await cli_flagger._run(Namespace(dry_run=False), config, FakeNotifier()) == 0
    assert closed == [True]
    assert caplog.text.count("error while closing a client") == 2
