import os
import stat

import pytest

from pdl_sms_store.cli_tokens import main
from pdl_sms_store.tokens import verify_token


def test_prints_token_to_stdout_and_hash_line_to_stderr(capsys):
    assert main(["--name", "pixel-1"]) == 0
    out, err = capsys.readouterr()
    token = out.strip()
    assert token
    prefix = "  pixel-1: "
    assert err.startswith(prefix)
    assert verify_token(token, err.removeprefix(prefix).strip())


def test_out_file_is_0600_and_not_printed(tmp_path, capsys):
    p = tmp_path / "tok"
    assert main(["--name", "pixel", "--out", str(p)]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    token = p.read_text().strip()
    assert verify_token(token, err.split(": ", 1)[1].strip())
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_refuses_overwrite(tmp_path, capsys):
    p = tmp_path / "tok"
    p.write_text("keep")
    assert main(["--name", "pixel", "--out", str(p)]) == 1
    assert p.read_text() == "keep"
    assert "already exists" in capsys.readouterr().err


def test_unwritable_out(tmp_path, capsys):
    assert main(["--name", "pixel", "--out", str(tmp_path / "no" / "dir" / "tok")]) == 1
    assert "cannot write" in capsys.readouterr().err


def test_bad_name_rejected():
    with pytest.raises(SystemExit):
        main(["--name", "Bad Name"])
