"""Command line helper ``pdl-sms-new-device-token``: create a device token and its hash."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from pdl_sms_store.tokens import hash_token, new_token

_NAME = re.compile(r"[a-z0-9_-]{1,32}")
_FILE_MODE = 0o600


def _name(value: str) -> str:
    if _NAME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("use [a-z0-9_-], 1-32 characters")
    return value


def _fail(message: str) -> int:
    sys.stderr.write(f"{message}\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdl-sms-new-device-token")
    parser.add_argument("--name", required=True, type=_name, help="device name")
    parser.add_argument("--out", type=Path, help="write the token to this new file (mode 0600)")
    args = parser.parse_args(argv)

    token = new_token()
    if args.out is not None:
        try:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        except FileExistsError:
            return _fail(f"{args.out} already exists; not overwriting")
        except OSError as exc:
            return _fail(f"cannot write {args.out}: {exc}")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    else:
        sys.stdout.write(token + "\n")
    sys.stderr.write(f"  {args.name}: {hash_token(token)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
