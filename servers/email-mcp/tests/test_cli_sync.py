import imaplib
import logging

import psycopg
import pytest

from pdl_email import cli_sync
from pdl_email.memory_store import MemoryStore
from pdl_email.store import StoreConfigError, SyncResult

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


def test_unreadable_password_file_exits_2(env, tmp_path, caplog):
    env.setenv("EMAIL_IMAP_PASSWORD_FILE", str(tmp_path / "absent"))
    assert cli_sync.main([]) == 2
    assert "EMAIL_IMAP_PASSWORD_FILE" in caplog.text


@pytest.mark.parametrize("content", [b"", bytes([10]), bytes([255, 254, 128])])
def test_empty_or_undecodable_password_file_exits_2(env, tmp_path, content):
    pw = tmp_path / "bad"
    pw.write_bytes(content)
    env.setenv("EMAIL_IMAP_PASSWORD_FILE", str(pw))
    assert cli_sync.main([]) == 2


@pytest.mark.parametrize(
    ("name", "value"),
    [("EMAIL_IMAP_PORT", "abc"), ("EMAIL_IMAP_PORT", "0"), ("EMAIL_INITIAL_DAYS", "-3")],
)
def test_bad_numeric_env_exits_2(env, caplog, name, value):
    env.setenv(name, value)
    assert cli_sync.main([]) == 2
    assert name in caplog.text


def test_once_ctrl_c_returns_130(env):
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    _no_source(env)
    env.setattr(cli_sync, "sync_once", interrupted)
    assert cli_sync.main([]) == 130


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


def test_store_factory_missing_exits_2(env, caplog):
    env.setenv("EMAIL_STORE", "postgres")

    def missing():
        raise ModuleNotFoundError("x", name="pdl_email.store_factory")

    env.setattr(cli_sync, "_load_store_factory", missing)
    assert cli_sync.main([]) == 2
    assert "EMAIL_STORE=memory" in caplog.text


@pytest.mark.parametrize(
    "exc", [StoreConfigError("PGUSER is required"), psycopg.OperationalError("refused")]
)
def test_store_startup_errors_exit_2(env, caplog, exc):
    env.setenv("EMAIL_STORE", "postgres")

    def boom(_env):
        raise exc

    env.setattr(cli_sync, "_load_store_factory", lambda: boom)
    assert cli_sync.main([]) == 2
    assert [r for r in caplog.records if r.levelno == logging.ERROR]


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
    outcomes = [
        OSError("x"),
        imaplib.IMAP4.error("y"),
        RuntimeError("malformed"),
        psycopg.OperationalError("db"),
        SyncResult(0, 0, False),
    ]
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
        if len(sleeps) == 5:
            raise KeyboardInterrupt

    _no_source(env)
    env.setattr(cli_sync, "sync_once", fake_sync)
    env.setattr(cli_sync.time, "sleep", fake_sleep)
    with caplog.at_level(logging.INFO):
        assert cli_sync.main(["--interval", "30"]) == 0
    assert len(runs) == 5 and sleeps == [30] * 5
    assert caplog.text.count("will retry") == 4
    assert "interrupted" in caplog.text


def test_interval_must_be_positive_and_exclusive(env):
    with pytest.raises(SystemExit):
        cli_sync.main(["--interval", "0"])
    with pytest.raises(SystemExit):
        cli_sync.main(["--once", "--interval", "5"])


def test_config_repr_hides_password(env):
    cfg = cli_sync._load_config()
    assert cfg is not None and cfg.password == "s3cret"
    assert "s3cret" not in repr(cfg)


def test_memory_store_selection_is_case_insensitive(env):
    env.setenv("EMAIL_STORE", " Memory ")
    assert isinstance(cli_sync._open_store(), MemoryStore)
