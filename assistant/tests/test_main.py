"""Tests for the ``pdl-assistant`` entry point."""

from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from starlette.applications import Starlette

from pdl_assistant import __main__ as entry
from pdl_assistant.memory import MemoryStore

COOKIE_SECRET = "s" * 40


def _write_files(tmp_path: Path, *, client_secret: bool = True) -> Path:
    secrets = tmp_path / "assistant-secrets.yaml"
    secrets.write_text(f"api_tokens: {{}}\ncookie_secret: {COOKIE_SECRET}\n", encoding="utf-8")
    if client_secret:
        (tmp_path / "client.secret").write_text("pa-secret\n", encoding="utf-8")
    config = tmp_path / "assistant.yaml"
    config.write_text(
        "\n".join(
            [
                "gateway_url: http://gateway.test",
                "ollama_url: http://ollama.test:11434",
                "model: qwen-test",
                f"client_secret_file: {(tmp_path / 'client.secret').as_posix()}",
                f"secrets_file: {secrets.as_posix()}",
                f"db_path: {(tmp_path / 'data' / 'assistant.db').as_posix()}",
                "public_url: https://assistant.test",
                "listen_port: 8799",
            ]
        ),
        encoding="utf-8",
    )
    return config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASSISTANT_CONFIG", "ASSISTANT_SECRETS", "ASSISTANT_SECRETS_FILE"):
        monkeypatch.delenv(name, raising=False)


def test_main_requires_client_secret_file(tmp_path, capsys, monkeypatch):
    config = _write_files(tmp_path, client_secret=False)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: pytest.fail("must not serve"))

    assert entry.main(["--config", str(config)]) == 1
    assert "client.secret" in capsys.readouterr().err


def test_main_empty_client_secret_is_1(tmp_path, capsys, monkeypatch):
    config = _write_files(tmp_path)
    (tmp_path / "client.secret").write_text("  \n", encoding="utf-8")
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: pytest.fail("must not serve"))

    assert entry.main(["--config", str(config)]) == 1
    assert "empty" in capsys.readouterr().err


def test_main_bad_config_is_1(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: pytest.fail("must not serve"))

    assert entry.main(["--config", str(tmp_path / "missing.yaml")]) == 1
    assert "pdl-assistant:" in capsys.readouterr().err


async def test_main_runs_uvicorn(tmp_path, monkeypatch):
    config = _write_files(tmp_path)
    seen: dict[str, Any] = {}

    def fake_run(app: Starlette, **kwargs: Any) -> None:
        seen["app"], seen["kwargs"] = app, kwargs

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    monkeypatch.setenv("ASSISTANT_CONFIG", str(config))

    assert entry.main([]) == 0

    assert seen["kwargs"] == {"host": "0.0.0.0", "port": 8799}
    assert isinstance(seen["app"], Starlette)
    async with LifespanManager(seen["app"]):  # shutdown closes the store and the clients
        pass
    assert (tmp_path / "data" / "assistant.db").exists()


async def test_main_secrets_flag_overrides_config(tmp_path, monkeypatch, capsys):
    config = _write_files(tmp_path)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: pytest.fail("must not serve"))

    code = entry.main(["--config", str(config), "--secrets", str(tmp_path / "nope.yaml")])

    assert code == 1
    assert "nope.yaml" in capsys.readouterr().err


async def test_shutdown_closes_everything_even_if_a_client_fails(tmp_path, monkeypatch):
    config = _write_files(tmp_path)
    seen: dict[str, Any] = {}
    closed: list[str] = []

    async def boom(self: Any) -> None:
        closed.append("login")
        raise RuntimeError("close failed")

    def recording_close(self: MemoryStore) -> None:
        closed.append("memory")
        real_close(self)

    real_close = MemoryStore.close
    monkeypatch.setattr(entry.GatewayLogin, "aclose", boom)
    monkeypatch.setattr(entry.MemoryStore, "close", recording_close)
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **k: seen.setdefault("app", app))

    assert entry.main(["--config", str(config)]) == 0
    async with LifespanManager(seen["app"]):
        pass
    assert closed == ["login", "memory"]
