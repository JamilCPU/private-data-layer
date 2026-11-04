import imaplib
import logging
import sys

import pytest

from pdl_email import cli_sync
from pdl_email.memory_store import MemoryStore
from pdl_email.store import SyncResult

REQUIRED = ["EMAIL_IMAP_HOST", "EMAIL_IMAP_USER", "EMAIL_IMAP_PASSWORD_FILE"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    values = {
        "EMAIL_IMAP_HOST": "imap.example.test",
        "EMAIL_IMAP_USER": "me",
        "EMAIL_IMAP_PASSWORD_FILE": str(pw),
        "EMAIL_STORE": "memory",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    for k in ("EMAIL_MAILBOX", "EMAIL_IMAP_PORT", "EMAIL_INITIAL_DAYS"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _no_source(env):
    env.setattr(cli_sync, "source_factory", lambda *a, **k: None)


@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_env_exits_2(env, missing):
    env.delenv(missing)
    assert cli_sync.main([]) == 2


def test_unreadable_password_file_exits_2(env, tmp_path):
    env.setenv("EMAIL_IMAP_PASSWORD_FILE", str(tmp_path / "absent"))
    assert cli_sync.main([]) == 2


def test_once_reads_password_and_succeeds(env, caplog):
    seen = {}
    env.setattr(cli_sync, "source_factory", lambda *a, **k: seen.update(args=a, kw=k) or "SRC")
    calls = {}

    def fake_sync(source, store, **kw):
        calls.update(source=source, store=store, kw=kw)
        return SyncResult(0, 0, False)

    env.setattr(cli_sync, "sync_once", fake_sync)
    env.setenv("EMAIL_MAILBOX", "Work")
    env.setenv("EMAIL_INITIAL_DAYS", "7")
    with caplog.at_level(logging.INFO):
        assert cli_sync.main(["--once"]) == 0
    assert seen["args"] == ("imap.example.test", "me", "s3cret")
    assert seen["kw"] == {"mailbox": "Work", "port": 993}
    assert isinstance(calls["store"], MemoryStore)
    assert calls["kw"] == {"mailbox": "Work", "initial_days": 7}
    assert "sync result" in caplog.text and "s3cret" not in caplog.text


def test_once_failure_exits_1(env):
    def boom(*a, **k):
        raise OSError("down")

    _no_source(env)
    env.setattr(cli_sync, "sync_once", boom)
    assert cli_sync.main([]) == 1


def test_store_factory_missing_exits_2(env, capsys):
    env.setenv("EMAIL_STORE", "postgres")
    env.delitem(sys.modules, "pdl_email.store_factory", raising=False)
    assert cli_sync.main([]) == 2
    assert "EMAIL_STORE=memory" in capsys.readouterr().err


def test_store_factory_seam_used(env):
    env.setenv("EMAIL_STORE", "postgres")
    mem = MemoryStore()
    env.setattr(cli_sync, "_load_store_factory", lambda: lambda _env: mem)
    got = {}
    _no_source(env)
    env.setattr(
        cli_sync, "sync_once", lambda s, st, **k: got.update(st=st) or SyncResult(0, 0, False)
    )
    assert cli_sync.main([]) == 0
    assert got["st"] is mem


def test_store_factory_other_import_error_propagates(env):
    env.setenv("EMAIL_STORE", "postgres")

    def bad():
        raise ModuleNotFoundError("nope", name="psycopg")

    env.setattr(cli_sync, "_load_store_factory", bad)
    with pytest.raises(ModuleNotFoundError):
        cli_sync.main([])


def test_interval_survives_errors_and_stops_on_ctrl_c(env, caplog):
    outcomes = [OSError("x"), imaplib.IMAP4.error("y"), SyncResult(0, 0, False)]
    runs = []

    def fake_sync(*a, **k):
        runs.append(1)
        out = outcomes[len(runs) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    _no_source(env)
    env.setattr(cli_sync, "sync_once", fake_sync)
    env.setattr(cli_sync.time, "sleep", fake_sleep)
    with caplog.at_level(logging.INFO):
        assert cli_sync.main(["--interval", "30"]) == 0
    assert len(runs) == 3 and sleeps == [30, 30, 30]
    assert "interrupted" in caplog.text


def test_interval_must_be_positive_and_exclusive(env):
    with pytest.raises(SystemExit):
        cli_sync.main(["--interval", "0"])
    with pytest.raises(SystemExit):
        cli_sync.main(["--once", "--interval", "5"])
