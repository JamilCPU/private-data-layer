"""main(): env validation and the uvicorn hand-off."""

import pytest

from pdl_search import __main__ as entry
from pdl_search.middleware import SharedSecretMiddleware

ENV = ("SEARCH_MCP_SHARED_SECRET", "SEARCH_MCP_HOST", "SEARCH_MCP_PORT", "SEARCH_SEARXNG_URL")
SEARX = "http://searxng:8080"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def runs(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    return calls


def test_main_requires_secret(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("SEARCH_SEARXNG_URL", SEARX)
    assert entry.main([]) == 1
    assert "search-mcp: " in capsys.readouterr().err
    assert runs == []


def test_main_requires_searxng_url(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("SEARCH_MCP_SHARED_SECRET", "s3cret")
    assert entry.main([]) == 1
    assert "SEARCH_SEARXNG_URL" in capsys.readouterr().err
    assert runs == []


@pytest.mark.parametrize("url", ["searxng:8080", "ftp://searxng", "http://"])
def test_main_rejects_non_http_searxng_url(monkeypatch, capsys, runs, url) -> None:
    monkeypatch.setenv("SEARCH_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("SEARCH_SEARXNG_URL", url)
    assert entry.main([]) == 1
    assert "SEARCH_SEARXNG_URL" in capsys.readouterr().err
    assert runs == []


def test_main_rejects_bad_port(monkeypatch, capsys, runs) -> None:
    monkeypatch.setenv("SEARCH_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("SEARCH_SEARXNG_URL", SEARX)
    monkeypatch.setenv("SEARCH_MCP_PORT", "eighty")
    assert entry.main([]) == 1
    assert "SEARCH_MCP_PORT" in capsys.readouterr().err
    assert runs == []


def test_main_runs_uvicorn_once_behind_secret(monkeypatch, runs) -> None:
    monkeypatch.setenv("SEARCH_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("SEARCH_SEARXNG_URL", SEARX)
    assert entry.main([]) == 0
    assert len(runs) == 1
    app, kwargs = runs[0]
    assert isinstance(app, SharedSecretMiddleware)
    assert kwargs == {"host": "0.0.0.0", "port": 8000}


def test_main_honours_host_and_port(monkeypatch, runs) -> None:
    monkeypatch.setenv("SEARCH_MCP_SHARED_SECRET", "s3cret")
    monkeypatch.setenv("SEARCH_SEARXNG_URL", SEARX)
    monkeypatch.setenv("SEARCH_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("SEARCH_MCP_PORT", "9100")
    assert entry.main([]) == 0
    assert runs[0][1] == {"host": "127.0.0.1", "port": 9100}
