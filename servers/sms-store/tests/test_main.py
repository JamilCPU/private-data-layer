import pytest
import uvicorn

from pdl_sms_store import __main__ as entry
from pdl_sms_store.tokens import hash_token


@pytest.fixture
def tokens_file(tmp_path):
    path = tmp_path / "tokens.yaml"
    path.write_text(f"devices:\n  pixel: {hash_token('t', iterations=10)}\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "SMS_STORE_LISTEN_HOST",
        "SMS_STORE_LISTEN_PORT",
        "SMS_DEVICE_TOKENS_FILE",
        "SMS_STORE",
        "SMS_APPLY_MIGRATIONS",
        "PGUSER",
        "PGPASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_tokens_env(capsys):
    assert entry.main() == 1
    assert "sms-store: " in capsys.readouterr().err


def test_missing_tokens_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SMS_DEVICE_TOKENS_FILE", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("SMS_STORE", "memory")
    assert entry.main() == 1
    assert capsys.readouterr().err.startswith("sms-store: ")


def test_bad_port(monkeypatch, tokens_file, capsys):
    monkeypatch.setenv("SMS_DEVICE_TOKENS_FILE", str(tokens_file))
    monkeypatch.setenv("SMS_STORE_LISTEN_PORT", "abc")
    assert entry.main() == 1
    assert "SMS_STORE_LISTEN_PORT" in capsys.readouterr().err


def test_bad_store_config(monkeypatch, tokens_file, capsys):
    monkeypatch.setenv("SMS_DEVICE_TOKENS_FILE", str(tokens_file))
    assert entry.main() == 1  # postgres default without PGUSER
    assert "PGUSER" in capsys.readouterr().err


def test_memory_store_runs(monkeypatch, tokens_file):
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    monkeypatch.setenv("SMS_DEVICE_TOKENS_FILE", str(tokens_file))
    monkeypatch.setenv("SMS_STORE", "memory")
    assert entry.main() == 0
    assert seen["host"] == "0.0.0.0" and seen["port"] == 8000
    assert seen["access_log"] is False
    assert seen["app"] is not None


def test_custom_host_port(monkeypatch, tokens_file):
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setenv("SMS_DEVICE_TOKENS_FILE", str(tokens_file))
    monkeypatch.setenv("SMS_STORE", "memory")
    monkeypatch.setenv("SMS_STORE_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("SMS_STORE_LISTEN_PORT", "9001")
    assert entry.main() == 0
    assert seen == {"host": "127.0.0.1", "port": 9001, "access_log": False}
