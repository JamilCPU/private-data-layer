"""Helper commands that create the hashed entries for ``secrets.yaml``.

``pdl-new-client-secret`` and ``pdl-set-passphrase`` print only the YAML lines to paste.
A plaintext secret goes to a 0600 file (``--out``) or, once, to an interactive terminal;
it is never written to a pipe or a redirected file.
"""

import argparse
import getpass
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from pdl_gateway.secrets import hash_secret, new_secret

_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_FILE_MODE = 0o600


def _client_id(value: str) -> str:
    if _CLIENT_ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "use letters, digits, '.', '_' and '-' only, starting with a letter or digit"
        )
    return value


def _write_new_file(path: Path, text: str) -> None:
    """Create ``path`` with mode 0600; fail if it already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _fail(message: str, code: int) -> int:
    sys.stderr.write(f"{message}\n")
    return code


def new_client_secret(argv: list[str] | None = None) -> int:
    """Generate a client secret; print the ``client_secrets`` line holding its hash."""
    parser = argparse.ArgumentParser(
        prog="pdl-new-client-secret", description="Create a client secret and its hash."
    )
    parser.add_argument("--client", required=True, type=_client_id, help="client id")
    parser.add_argument("--out", type=Path, help="write the secret to this new file (mode 0600)")
    args = parser.parse_args(argv)

    interactive = sys.stdout.isatty()
    if args.out is None and not interactive:
        return _fail(
            "refusing to print a secret to a non-terminal; pass --out PATH to save it to a file",
            2,
        )

    secret = new_secret()
    if args.out is not None:
        try:
            _write_new_file(args.out, secret + "\n")
        except FileExistsError:
            return _fail(f"{args.out} already exists; not overwriting", 1)
        except OSError as exc:
            return _fail(f"cannot write {args.out}: {exc}", 1)

    if args.out is None:
        sys.stdout.write(f"client secret (shown once): {secret}\n")
    sys.stdout.write("# add under client_secrets:\n")
    sys.stdout.write(f"  {args.client}: {hash_secret(secret)}\n")
    return 0


def set_approval_passphrase(
    argv: list[str] | None = None,
    *,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> int:
    """Ask for the approval passphrase twice; print the ``approval_passphrase`` line."""
    parser = argparse.ArgumentParser(
        prog="pdl-set-passphrase", description="Hash the approval passphrase for secrets.yaml."
    )
    parser.parse_args(argv)

    first = getpass_fn("Approval passphrase: ")
    second = getpass_fn("Repeat: ")
    if first != second:
        return _fail("passphrases differ", 1)
    if not first:
        return _fail("passphrase must not be empty", 1)
    sys.stdout.write(f"approval_passphrase: {hash_secret(first)}\n")
    return 0
