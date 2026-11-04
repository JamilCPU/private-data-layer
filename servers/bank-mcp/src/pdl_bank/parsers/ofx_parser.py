"""OFX/QFX statement parser (backed by ofxparse)."""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

from ofxparse import OfxParser

from pdl_bank.models import Account, Transaction, transaction_id
from pdl_bank.parsers.base import ParsedStatement, ParseError, check_size

log = logging.getLogger(__name__)


def parse_ofx(path: Path) -> ParsedStatement:
    path = Path(path)
    check_size(path)
    try:
        with path.open("rb") as fh, warnings.catch_warnings():
            # ofxparse still calls the bs4 findAll alias that bs4 >= 4.13 deprecates
            warnings.filterwarnings("ignore", message="Call to deprecated method findAll")
            ofx = OfxParser.parse(fh)
    except Exception as exc:  # ofxparse raises assorted errors on malformed input
        raise ParseError(f"{path.name}: cannot parse OFX: {exc}") from exc
    source = ofx.account
    if source is None or not source.account_id:
        raise ParseError(f"{path.name}: OFX file has no account")
    institution = getattr(source.institution, "organization", None) or None
    account = Account(
        id=str(source.account_id),
        name=getattr(source, "account_type", None) or "account",
        institution=institution,
        currency=getattr(source.statement, "currency", None) or "USD",
    )
    txs = tuple(_convert(account.id, t, path.name) for t in source.statement.transactions)
    log.info("parsed %d OFX transactions from %s", len(txs), path.name)
    return ParsedStatement(account, txs)


def _convert(account_id: str, raw, source_file: str) -> Transaction:
    description = f"{raw.payee or ''} {raw.memo or ''}".strip()
    posted_on = raw.date.date()
    return Transaction(
        id=transaction_id(account_id, posted_on, raw.amount, description),
        account_id=account_id,
        posted_on=posted_on,
        amount=raw.amount,
        description=description,
        category=None,
        source_file=source_file,
    )
