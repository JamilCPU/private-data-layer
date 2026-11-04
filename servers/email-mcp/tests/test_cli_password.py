import os
import stat
import sys

import pytest

from pdl_email import cli_password


def test_writes_file_with_newline(monkeypatch, tmp_path, capsys):
    out = tmp_path / "pw"
    monkeypatch.setattr(cli_password, "prompt", lambda _p: "app-pass-123")
    assert cli_password.main(["--out", str(out)]) == 0
    assert out.read_text() == "app-pass-123\n"
    printed = capsys.readouterr().out
    assert str(out) in printed and "app-pass-123" not in printed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_mode_is_0600(monkeypatch, tmp_path):
    out = tmp_path / "pw"
    monkeypatch.setattr(cli_password, "prompt", lambda _p: "x")
    cli_password.main(["--out", str(out)])
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600


def test_refuses_overwrite(monkeypatch, tmp_path, capsys):
    out = tmp_path / "pw"
    out.write_text("old")
    monkeypatch.setattr(cli_password, "prompt", lambda _p: "new")
    assert cli_password.main(["--out", str(out)]) == 1
    assert out.read_text() == "old"
    assert "refusing" in capsys.readouterr().err


def test_empty_input_exits_1(monkeypatch, tmp_path):
    out = tmp_path / "pw"
    monkeypatch.setattr(cli_password, "prompt", lambda _p: "  ")
    assert cli_password.main(["--out", str(out)]) == 1
    assert not out.exists()


def test_unwritable_path_exits_1(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_password, "prompt", lambda _p: "x")
    assert cli_password.main(["--out", str(tmp_path / "no" / "dir" / "pw")]) == 1
