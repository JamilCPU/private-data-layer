"""Pick a parser by file extension."""

from __future__ import annotations

from pathlib import Path

from pdl_bank.parsers.base import ParsedStatement, ParseError
from pdl_bank.parsers.csv_parser import CsvMapping, parse_csv
from pdl_bank.parsers.ofx_parser import parse_ofx


def detect_and_parse(path: Path, mappings: tuple[CsvMapping, ...]) -> ParsedStatement:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in {".csv", ".ofx", ".qfx"}:
        raise ParseError(f"{path.name}: unsupported file type {suffix!r}")
    if suffix == ".csv":
        return parse_csv(path, mappings)
    return parse_ofx(path)
