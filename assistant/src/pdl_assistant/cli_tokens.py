"""``pdl-assistant-new-api-token``: create an API token for the OpenAI-compatible endpoint.

Prints only the ``api_tokens`` YAML line holding the token's hash. The plaintext token goes
to a new 0600 file (``--out``) or, once, to an interactive terminal; it is never written to
a pipe or a redirected file. Mirrors ``pdl_gateway.cli_secrets``.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from .secrets import hash_secret, new_secret

_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
_FILE_MODE = 0o600


def _name(value: str) -> str:
    if _NAME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("use 1-40 letters, digits, '_' or '-'")
    return value


def _write_new_file(path: Path, text: str) -> None:
    """Create ``path`` with mode 0600; fail if it already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _fail(message: str, code: int) -> int:
    sys.stderr.write(f"{message}\n")
    return code


def new_api_token(argv: list[str] | None = None) -> int:
    """Generate an API token; print the ``api_tokens`` line holding its hash."""
    parser = argparse.ArgumentParser(
        prog="pdl-assistant-new-api-token", description="Create an API token and its hash."
    )
    parser.add_argument("--name", required=True, type=_name, help="token name, e.g. openwebui")
    parser.add_argument("--out", type=Path, help="write the token to this new file (mode 0600)")
    args = parser.parse_args(argv)

    if args.out is None and not sys.stdout.isatty():
        return _fail(
            "refusing to print a token to a non-terminal; pass --out PATH to save it to a file",
            2,
        )

    token = new_secret()
    if args.out is not None:
        try:
            _write_new_file(args.out, token + "\n")
        except FileExistsError:
            return _fail(f"{args.out} already exists; not overwriting", 1)
        except OSError as exc:
            return _fail(f"cannot write {args.out}: {exc}", 1)
    else:
        sys.stdout.write(f"api token (shown once): {token}\n")
    sys.stdout.write("# add under api_tokens:\n")
    sys.stdout.write(f"  {args.name}: {hash_secret(token)}\n")
    return 0
