"""Command line helper: ``pdl-email-set-password``.

Writes the IMAP app password to a new file. The file is created with mode 0600 on
POSIX; on Windows the mode is best effort (the OS largely ignores it).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from pathlib import Path

prompt: Callable[[str], str] = getpass.getpass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pdl-email-set-password")
    p.add_argument("--out", type=Path, required=True, help="file to create (must not exist)")
    args = p.parse_args(argv)
    out: Path = args.out
    if out.exists():
        print(f"refusing to overwrite existing file: {out}", file=sys.stderr)
        return 1
    secret = prompt("IMAP app password: ").strip()
    if not secret:
        print("empty password; nothing written", file=sys.stderr)
        return 1
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        print(f"cannot create {out}: {exc}", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret + "\n")
    print(f"password written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
