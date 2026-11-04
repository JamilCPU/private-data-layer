"""main(): env validation, store selection and the uvicorn hand-off."""

import psycopg
import pytest

from pdl_email import __main__ as entry
from pdl_email.memory_store import MemoryStore
from pdl_email.middleware import SharedSecretMiddleware
from pdl_email.store import StoreConfigError

ENV = ("EMAIL_MCP_SHARED_SECRET", "EMAIL_MCP_HOST", "EMAIL_MCP_PORT", "EMAIL_STORE")


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
    assert "email-mcp: " in capsys.readouterr().err


def test_main_memory_store_runs_uvicorn(monkeypatch, runs) -> None:
    monkeypatch.setenv("EMAIL_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("EMAIL_STORE", "memory")
    assert entry.main([]) == 0
    app, kwargs = runs[-1]
    assert isinstance(app, SharedSecretMiddleware)
    assert kwargs == {"host": "0.0.0.0", "port": 8000}
    monkeypatch.setenv("EMAIL_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("EMAIL_MCP_PORT", "9100")
    assert entry.main([]) == 0
    assert runs[-1][1] == {"host": "127.0.0.1", "port": 9100}
    assert len(runs) == 2


def test_main_rejects_bad_port(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("EMAIL_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("EMAIL_STORE", "memory")
    monkeypatch.setenv("EMAIL_MCP_PORT", "eighty")
    assert entry.main([]) == 1
    assert "EMAIL_MCP_PORT" in capsys.readouterr().err
    assert runs == []


def test_main_postgres_without_factory_fails(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("EMAIL_MCP_SHARED_SECRET", "s3cret")

    def missing():
        raise ModuleNotFoundError("no store_factory", name="pdl_email.store_factory")

    monkeypatch.setattr(entry, "_load_store_factory", missing)
    assert entry.main([]) == 1
    assert "EMAIL_STORE=memory" in capsys.readouterr().err
    assert runs == []


def test_main_postgres_uses_factory(monkeypatch, runs) -> None:
    monkeypatch.setenv("EMAIL_MCP_SHARED_SECRET", "s3cret")
    seen: list[object] = []

    def factory(env):
        seen.append(env)
        return MemoryStore()

    monkeypatch.setattr(entry, "_load_store_factory", lambda: factory)
    assert entry.main([]) == 0
    assert len(seen) == 1
    assert len(runs) == 1


def test_main_rejects_invalid_store(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("EMAIL_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("EMAIL_STORE", "sqlite")
    assert entry.main([]) == 1
    assert "EMAIL_STORE must be memory or postgres" in capsys.readouterr().err
    assert runs == []


def test_main_unrelated_missing_module_propagates(monkeypatch, runs) -> None:
    monkeypatch.setenv("EMAIL_MCP_SHARED_SECRET", "s3cret")

    def broken():
        raise ModuleNotFoundError("No module named 'psycopg_pool'", name="psycopg_pool")

    monkeypatch.setattr(entry, "_load_store_factory", broken)
    with pytest.raises(ModuleNotFoundError):
        entry.main([])
    assert runs == []


@pytest.mark.parametrize(
    "error",
    [StoreConfigError("EMAIL_DB_HOST is required"), psycopg.OperationalError("connection refused")],
)
def test_main_store_open_failure_exits_cleanly(monkeypatch, capsys, runs, error) -> None:
    monkeypatch.setenv("EMAIL_MCP_SHARED_SECRET", "s3cret")

    def factory(env):
        raise error

    monkeypatch.setattr(entry, "_load_store_factory", lambda: factory)
    assert entry.main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("email-mcp: ") and str(error) in err
    assert runs == []
