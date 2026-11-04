"""Shared parser types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pdl_bank.models import Account, Transaction

MAX_FILE_BYTES = 20_000_000


class ParseError(ValueError):
    """Raised when a statement file cannot be parsed."""


@dataclass(frozen=True)
class ParsedStatement:
    account: Account
    transactions: tuple[Transaction, ...]


def check_size(path: Path) -> None:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ParseError(f"{path.name}: file too large ({size} > {MAX_FILE_BYTES} bytes)")
