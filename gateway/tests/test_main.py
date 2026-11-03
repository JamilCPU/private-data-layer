"""Tests of the ``pdl-gateway`` entry point."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.applications import Starlette

from pdl_gateway import __main__ as entry
from pdl_gateway.secrets import hash_secret

from .conftest import EXAMPLE_CONFIG, PA_SECRET


def _write_files(tmp_path: Path) -> tuple[Path, Path]:
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    raw["db_path"] = str(tmp_path / "gateway.db")
    raw["audit_dir"] = str(tmp_path / "audit")
    for backend in raw["backends"]:
        backend.pop("ca_file", None)
    config_path = tmp_path / "gateway.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    secrets_raw = {
        "approval_passphrase": hash_secret("pass", iterations=1000),
        "client_secrets": {"personal-assistant": hash_secret(PA_SECRET, iterations=1000)},
    }
    secrets_path = tmp_path / "secrets.yaml"
    secrets_path.write_text(yaml.safe_dump(secrets_raw), encoding="utf-8")
    return config_path, secrets_path


def test_main_bad_config_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope.yaml"
    assert entry.main(["--config", str(missing), "--secrets", str(missing)]) == 1
    assert "nope.yaml" in capsys.readouterr().err


def test_main_missing_backend_env_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, secrets_path = _write_files(tmp_path)
    monkeypatch.delenv("HOMESTUDIO_MCP_SECRET", raising=False)
    monkeypatch.setenv("GATEWAY_CONFIG", str(config_path))
    monkeypatch.setenv("GATEWAY_SECRETS", str(secrets_path))
    assert entry.main([]) == 1
    assert "HOMESTUDIO_MCP_SECRET" in capsys.readouterr().err


def test_main_runs_uvicorn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path, secrets_path = _write_files(tmp_path)
    monkeypatch.setenv("HOMESTUDIO_MCP_SECRET", "s1")
    monkeypatch.setenv("AUTOMATIONS_MCP_TOKEN", "t1")
    calls: list[tuple[Any, str, int]] = []

    def fake_run(app: Any, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    assert entry.main(["--config", str(config_path), "--secrets", str(secrets_path)]) == 0
    [(app, host, port)] = calls
    assert isinstance(app, Starlette)
    assert (host, port) == ("0.0.0.0", 8787)


def test_main_utf16_config_returns_1_with_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, secrets_path = _write_files(tmp_path)
    config_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-16")
    assert entry.main(["--config", str(config_path), "--secrets", str(secrets_path)]) == 1
    err = capsys.readouterr().err
    assert "not UTF-8 text" in err
    assert "Set-Content -Encoding utf8" in err
