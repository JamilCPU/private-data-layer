import stat
import sys

import pytest

from pdl_gateway.cli_secrets import new_client_secret, set_approval_passphrase
from pdl_gateway.secrets import verify_secret


def _tty(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


def test_new_client_secret_writes_file_and_prints_hash(tmp_path, capsys, monkeypatch):
    _tty(monkeypatch, False)
    out = tmp_path / "pa.secret"

    code = new_client_secret(["--client", "personal-assistant", "--out", str(out)])

    captured = capsys.readouterr()
    assert code == 0
    secret = out.read_text(encoding="utf-8").strip()
    assert len(secret) >= 40
    assert secret not in captured.out and secret not in captured.err
    lines = captured.out.splitlines()
    assert lines[0] == "# add under client_secrets:"
    prefix = "  personal-assistant: "
    assert lines[1].startswith(prefix)
    encoded = lines[1][len(prefix) :]
    assert encoded.startswith("pbkdf2_sha256$200000$")
    assert verify_secret(secret, encoded)
    if sys.platform != "win32":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_new_client_secret_refuses_to_overwrite(tmp_path, capsys, monkeypatch):
    _tty(monkeypatch, False)
    out = tmp_path / "pa.secret"
    out.write_text("keep me", encoding="utf-8")

    code = new_client_secret(["--client", "pa", "--out", str(out)])

    captured = capsys.readouterr()
    assert code == 1
    assert out.read_text(encoding="utf-8") == "keep me"
    assert "already exists" in captured.err
    assert captured.out == ""


def test_new_client_secret_refuses_to_print_when_piped(monkeypatch, capsys):
    _tty(monkeypatch, False)

    code = new_client_secret(["--client", "pa"])

    captured = capsys.readouterr()
    assert code == 2
    assert "--out" in captured.err
    assert captured.out == ""


def test_new_client_secret_prints_once_on_a_terminal(monkeypatch, capsys):
    _tty(monkeypatch, True)

    code = new_client_secret(["--client", "pa"])

    captured = capsys.readouterr()
    assert code == 0
    assert "# add under client_secrets:" in captured.out
    hash_line = next(line for line in captured.out.splitlines() if line.startswith("  pa: "))
    secret_line = next(
        line for line in captured.out.splitlines() if line.startswith("client secret")
    )
    secret = secret_line.split()[-1]
    assert verify_secret(secret, hash_line[len("  pa: ") :])
    assert captured.err == ""


@pytest.mark.parametrize("bad", ["", "has space", "a:b", "-lead", "new\nline"])
def test_new_client_secret_rejects_unsafe_client_ids(bad, monkeypatch, capsys):
    _tty(monkeypatch, True)
    with pytest.raises(SystemExit) as exc:
        new_client_secret(["--client", bad])
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""


def _answers(*values: str):
    queue = list(values)
    prompts: list[str] = []

    def getpass_fn(prompt: str) -> str:
        prompts.append(prompt)
        return queue.pop(0)

    return getpass_fn, prompts


def test_set_passphrase_mismatch(capsys):
    getpass_fn, _ = _answers("a", "b")

    assert set_approval_passphrase([], getpass_fn=getpass_fn) == 1

    captured = capsys.readouterr()
    assert "passphrases differ" in captured.err
    assert captured.out == ""


def test_set_passphrase_empty(capsys):
    getpass_fn, _ = _answers("", "")

    assert set_approval_passphrase([], getpass_fn=getpass_fn) == 1

    captured = capsys.readouterr()
    assert "passphrase must not be empty" in captured.err
    assert captured.out == ""


def test_set_passphrase_prints_hash(capsys):
    getpass_fn, prompts = _answers("pw", "pw")

    assert set_approval_passphrase([], getpass_fn=getpass_fn) == 0

    captured = capsys.readouterr()
    assert prompts == ["Approval passphrase: ", "Repeat: "]
    line = captured.out.strip()
    assert captured.out.count("\n") == 1
    assert line.startswith("approval_passphrase: pbkdf2_sha256$200000$")
    assert verify_secret("pw", line.removeprefix("approval_passphrase: "))
    assert "pw" not in captured.err
