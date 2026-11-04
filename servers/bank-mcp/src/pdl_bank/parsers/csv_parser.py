"""CSV statement parser driven by YAML header mappings."""

from __future__ import annotations

import csv
import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from pdl_bank.models import Account, Transaction, normalise_description, transaction_id
from pdl_bank.parsers.base import ParsedStatement, ParseError, check_size

log = logging.getLogger(__name__)
_STRIP = re.compile(r"[$,\s]")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


@dataclass(frozen=True)
class CsvMapping:
    name: str
    match_header: tuple[str, ...]
    date_col: str
    date_format: str
    description_col: str
    amount_col: str | None
    debit_col: str | None
    credit_col: str | None
    account_name: str
    institution: str | None
    negate: bool = False


def _mapping_from(index: int, entry: object) -> CsvMapping:
    if not isinstance(entry, dict):
        raise ParseError(f"mapping #{index}: expected a mapping")
    try:
        m = CsvMapping(
            name=str(entry["name"]),
            match_header=tuple(str(c) for c in entry["match_header"]),
            date_col=entry["date_col"],
            date_format=entry["date_format"],
            description_col=entry["description_col"],
            amount_col=entry.get("amount_col"),
            debit_col=entry.get("debit_col"),
            credit_col=entry.get("credit_col"),
            account_name=entry["account_name"],
            institution=entry.get("institution"),
            negate=bool(entry.get("negate", False)),
        )
    except (KeyError, TypeError) as exc:
        raise ParseError(f"mapping #{index}: missing or invalid key {exc}") from exc
    split = m.debit_col is not None and m.credit_col is not None
    if (m.amount_col is not None) == split:
        raise ParseError(
            f"mapping {m.name!r}: set amount_col, or both debit_col and credit_col (not both)"
        )
    return m


def load_mappings(path: Path) -> tuple[CsvMapping, ...]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ParseError(f"mappings file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mappings"), list):
        raise ParseError("mappings file must be a mapping with a 'mappings' list")
    return tuple(_mapping_from(i, e) for i, e in enumerate(data["mappings"], start=1))


def _to_decimal(raw: str) -> Decimal | None:
    text = _STRIP.sub("", raw)
    if not text:
        return None
    wrapped = text.startswith("(") and text.endswith(")")
    inner = text[1:-1] if wrapped else text
    if not _NUMBER.fullmatch(inner):
        raise ParseError(f"invalid amount {raw!r}")
    try:
        value = Decimal(inner)
    except InvalidOperation as exc:  # pragma: no cover - regex already guarantees validity
        raise ParseError(f"invalid amount {raw!r}") from exc
    return -abs(value) if wrapped else value


def _row_amount(row: dict[str, str], m: CsvMapping) -> Decimal | None:
    if m.amount_col is not None:
        amount = _to_decimal(row.get(m.amount_col, ""))
    else:
        debit = _to_decimal(row.get(m.debit_col or "", ""))
        credit = _to_decimal(row.get(m.credit_col or "", ""))
        if debit is None and credit is None:
            return None
        amount = abs(credit or Decimal(0)) - abs(debit or Decimal(0))
    if amount is not None and m.negate:
        amount = -amount
    return amount


def _row_date(row: dict[str, str], m: CsvMapping) -> date:
    raw = row.get(m.date_col, "").strip()
    try:
        return datetime.strptime(raw, m.date_format).date()
    except ValueError as exc:
        raise ParseError(f"invalid date {raw!r} for format {m.date_format!r}") from exc


def _find_mapping(header: list[str], mappings: tuple[CsvMapping, ...]) -> CsvMapping:
    cols = {h.strip() for h in header}
    for m in mappings:
        if all(c in cols for c in m.match_header):
            return m
    raise ParseError(f"no CSV mapping matches header {header}")


def parse_csv(path: Path, mappings: tuple[CsvMapping, ...]) -> ParsedStatement:
    path = Path(path)
    check_size(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        mapping = _find_mapping(list(reader.fieldnames or []), mappings)
        key = f"{mapping.name}|{mapping.account_name}".encode()
        account = Account(
            id=hashlib.sha256(key).hexdigest()[:16],
            name=mapping.account_name,
            institution=mapping.institution,
        )
        txs = _read_rows(reader, mapping, account.id, path.name)
    log.info("parsed %d CSV transactions from %s", len(txs), path.name)
    return ParsedStatement(account, txs)


def _read_rows(
    reader: csv.DictReader, m: CsvMapping, account_id: str, source_file: str
) -> tuple[Transaction, ...]:
    out: list[Transaction] = []
    seen: Counter[tuple] = Counter()
    for raw in reader:
        line = reader.line_num
        row = {(k or "").strip(): (v or "") for k, v in raw.items()}
        try:
            amount = _row_amount(row, m)
            if amount is None:
                log.warning("%s line %d: blank amount, row skipped", source_file, line)
                continue
            posted_on = _row_date(row, m)
        except ParseError as exc:
            raise ParseError(f"{source_file} line {line}: {exc}") from exc
        desc = row.get(m.description_col, "").strip()
        key = (account_id, posted_on, amount, normalise_description(desc))
        seen[key] += 1
        out.append(
            Transaction(
                id=transaction_id(account_id, posted_on, amount, desc, seen[key]),
                account_id=account_id,
                posted_on=posted_on,
                amount=amount,
                description=desc,
                category=None,
                source_file=source_file,
            )
        )
    return tuple(out)
