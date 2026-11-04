"""main(): env validation, store selection and the uvicorn hand-off."""

import pytest

from pdl_bank import __main__ as entry
from pdl_bank.memory_store import MemoryStore
from pdl_bank.middleware import SharedSecretMiddleware

ENV = ("BANK_MCP_SHARED_SECRET", "BANK_MCP_HOST", "BANK_MCP_PORT", "BANK_STORE")


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
    assert "bank-mcp: " in capsys.readouterr().err


def test_main_memory_store_runs_uvicorn(monkeypatch, runs) -> None:
    monkeypatch.setenv("BANK_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("BANK_STORE", "memory")
    assert entry.main([]) == 0
    app, kwargs = runs[-1]
    assert isinstance(app, SharedSecretMiddleware)
    assert kwargs == {"host": "0.0.0.0", "port": 8000}
    monkeypatch.setenv("BANK_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("BANK_MCP_PORT", "9100")
    assert entry.main([]) == 0
    assert runs[-1][1] == {"host": "127.0.0.1", "port": 9100}
    assert len(runs) == 2


def test_main_rejects_bad_port(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("BANK_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("BANK_STORE", "memory")
    monkeypatch.setenv("BANK_MCP_PORT", "eighty")
    assert entry.main([]) == 1
    assert "BANK_MCP_PORT" in capsys.readouterr().err
    assert runs == []


def test_main_postgres_without_factory_fails(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("BANK_MCP_SHARED_SECRET", "s3cret")

    def missing():
        raise ImportError("no store_factory")

    monkeypatch.setattr(entry, "_load_store_factory", missing)
    assert entry.main([]) == 1
    assert "BANK_STORE=memory" in capsys.readouterr().err
    assert runs == []


def test_main_postgres_uses_factory(monkeypatch, runs) -> None:
    monkeypatch.setenv("BANK_MCP_SHARED_SECRET", "s3cret")
    seen: list[object] = []

    def factory(env):
        seen.append(env)
        return MemoryStore()

    monkeypatch.setattr(entry, "_load_store_factory", lambda: factory)
    assert entry.main([]) == 0
    assert len(seen) == 1
    assert len(runs) == 1
