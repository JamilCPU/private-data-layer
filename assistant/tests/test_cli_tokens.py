"""Tests for ``pdl-assistant-new-api-token``."""

import stat
import sys

import pytest

from pdl_assistant.cli_tokens import new_api_token
from pdl_assistant.secrets import verify_secret


def _tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


def test_writes_file_and_prints_hash(tmp_path, capsys, monkeypatch):
    _tty(monkeypatch, False)
    out = tmp_path / "openwebui.token"

    code = new_api_token(["--name", "openwebui", "--out", str(out)])

    captured = capsys.readouterr()
    assert code == 0
    token = out.read_text(encoding="utf-8").strip()
    assert len(token) >= 40
    assert token not in captured.out and token not in captured.err
    lines = captured.out.splitlines()
    assert lines[0] == "# add under api_tokens:"
    prefix = "  openwebui: "
    assert lines[1].startswith(prefix)
    encoded = lines[1][len(prefix) :]
    assert encoded.startswith("pbkdf2_sha256$200000$")
    assert verify_secret(token, encoded)
    if sys.platform != "win32":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_refuses_to_overwrite(tmp_path, capsys, monkeypatch):
    _tty(monkeypatch, False)
    out = tmp_path / "t.token"
    out.write_text("keep me", encoding="utf-8")

    code = new_api_token(["--name", "phone", "--out", str(out)])

    captured = capsys.readouterr()
    assert code == 1
    assert out.read_text(encoding="utf-8") == "keep me"
    assert "already exists" in captured.err
    assert captured.out == ""


def test_unwritable_out_is_1(tmp_path, capsys, monkeypatch):
    _tty(monkeypatch, False)

    code = new_api_token(["--name", "phone", "--out", str(tmp_path / "missing" / "t.token")])

    captured = capsys.readouterr()
    assert code == 1
    assert "cannot write" in captured.err
    assert captured.out == ""


def test_refuses_to_print_when_piped(monkeypatch, capsys):
    _tty(monkeypatch, False)

    code = new_api_token(["--name", "phone"])

    captured = capsys.readouterr()
    assert code == 2
    assert "--out" in captured.err
    assert captured.out == ""


def test_prints_once_on_a_terminal(monkeypatch, capsys):
    _tty(monkeypatch, True)

    code = new_api_token(["--name", "phone"])

    captured = capsys.readouterr()
    assert code == 0
    assert "# add under api_tokens:" in captured.out
    lines = captured.out.splitlines()
    hash_line = next(line for line in lines if line.startswith("  phone: "))
    token_line = next(line for line in lines if line.startswith("api token"))
    assert verify_secret(token_line.split()[-1], hash_line[len("  phone: ") :])
    assert captured.err == ""


@pytest.mark.parametrize("bad", ["", "has space", "a:b", "x" * 41, "new\nline", "dot.ted"])
def test_rejects_unsafe_names(bad, monkeypatch, capsys):
    _tty(monkeypatch, True)

    with pytest.raises(SystemExit) as excinfo:
        new_api_token(["--name", bad])

    assert excinfo.value.code == 2
    assert capsys.readouterr().out == ""


def test_name_is_required(monkeypatch, capsys):
    _tty(monkeypatch, True)
    with pytest.raises(SystemExit):
        new_api_token([])
