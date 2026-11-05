"""main(): env validation, store selection and the uvicorn hand-off."""

import psycopg
import pytest

from pdl_sms import __main__ as entry
from pdl_sms.memory_store import MemoryStore
from pdl_sms.middleware import SharedSecretMiddleware
from pdl_sms.store import StoreConfigError

ENV = ("SMS_MCP_SHARED_SECRET", "SMS_MCP_HOST", "SMS_MCP_PORT", "SMS_STORE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def runs(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    return calls


def test_main_requires_secret(capsys) -> None:
    assert entry.main([]) == 1
    assert "sms-mcp: " in capsys.readouterr().err


def test_main_memory_store_runs_uvicorn(monkeypatch, runs) -> None:
    monkeypatch.setenv("SMS_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("SMS_STORE", "memory")
    assert entry.main([]) == 0
    app, kwargs = runs[-1]
    assert isinstance(app, SharedSecretMiddleware)
    assert kwargs == {"host": "0.0.0.0", "port": 8000}
    monkeypatch.setenv("SMS_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMS_MCP_PORT", "9100")
    assert entry.main([]) == 0
    assert runs[-1][1] == {"host": "127.0.0.1", "port": 9100}


def test_main_rejects_bad_port(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("SMS_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("SMS_STORE", "memory")
    monkeypatch.setenv("SMS_MCP_PORT", "eighty")
    assert entry.main([]) == 1
    assert "SMS_MCP_PORT" in capsys.readouterr().err
    assert runs == []


def test_main_postgres_uses_factory(monkeypatch, runs) -> None:
    monkeypatch.setenv("SMS_MCP_SHARED_SECRET", "s3cret")
    seen: list[object] = []

    def factory(env):
        seen.append(env)
        return MemoryStore()

    monkeypatch.setattr(entry, "store_from_env", factory)
    assert entry.main([]) == 0
    assert len(seen) == 1
    assert len(runs) == 1


def test_main_rejects_invalid_store(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("SMS_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("SMS_STORE", "sqlite")
    assert entry.main([]) == 1
    assert "SMS_STORE must be memory or postgres" in capsys.readouterr().err
    assert runs == []


@pytest.mark.parametrize(
    "error",
    [StoreConfigError("PGUSER is required"), psycopg.OperationalError("connection refused")],
)
def test_main_store_open_failure_exits_cleanly(monkeypatch, capsys, runs, error) -> None:
    monkeypatch.setenv("SMS_MCP_SHARED_SECRET", "s3cret")

    def factory(env):
        raise error

    monkeypatch.setattr(entry, "store_from_env", factory)
    assert entry.main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("sms-mcp: ") and str(error) in err
    assert runs == []
