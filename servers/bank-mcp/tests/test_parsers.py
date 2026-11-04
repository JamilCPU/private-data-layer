import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pdl_bank.parsers import (
    MAX_FILE_BYTES,
    CsvMapping,
    ParseError,
    detect_and_parse,
    load_mappings,
    parse_csv,
    parse_ofx,
)

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
MAPPINGS = load_mappings(ROOT / "csv-mappings.example.yaml")


def _total(stmt) -> Decimal:
    return sum((t.amount for t in stmt.transactions), Decimal(0))


def test_csv_single_amount_fixture(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        stmt = detect_and_parse(FIX / "sample.csv", MAPPINGS)
    assert len(stmt.transactions) == 4  # blank-amount row skipped
    assert "blank amount" in caplog.text
    assert _total(stmt) == Decimal("3673.51")
    first = stmt.transactions[0]
    assert first.amount == Decimal("1234.50")
    assert first.posted_on == date(2026, 1, 3)
    assert first.category is None and first.source_file == "sample.csv"
    assert stmt.account.institution == "Example Bank"
    assert stmt.account.name == "Everyday Checking"
    assert len({t.id for t in stmt.transactions}) == 4


def test_csv_debit_credit_fixture() -> None:
    stmt = parse_csv(FIX / "sample_debit_credit.csv", MAPPINGS)
    assert [t.amount for t in stmt.transactions] == [
        Decimal("-52.30"),
        Decimal("2000.00"),
        Decimal("-45.00"),
    ]
    assert stmt.transactions[0].posted_on == date(2026, 1, 3)
    assert all(isinstance(t.amount, Decimal) for t in stmt.transactions)


def test_parentheses_negative_and_currency_symbols() -> None:
    stmt = parse_csv(FIX / "sample.csv", MAPPINGS)
    by_desc = {t.description: t.amount for t in stmt.transactions}
    assert by_desc["STREAMFLIX MONTHLY"] == Decimal("-15.99")
    assert by_desc["GREEN GROCER #12"] == Decimal("1234.50")


def test_csv_negate_and_bom(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("\ufeffD,Desc,Amt\n2026-02-01,SHOP,10.00\n", encoding="utf-8")
    m = CsvMapping(
        name="n",
        match_header=("D", "Desc", "Amt"),
        date_col="D",
        date_format="%Y-%m-%d",
        description_col="Desc",
        amount_col="Amt",
        debit_col=None,
        credit_col=None,
        account_name="Card",
        institution=None,
        negate=True,
    )
    stmt = parse_csv(p, (m,))
    assert stmt.transactions[0].amount == Decimal("-10.00")


def test_bad_csv_values_are_parse_errors(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv"
    p.write_text("Date,Description,Amount\nnot-a-date,X,1.00\n", encoding="utf-8")
    with pytest.raises(ParseError):
        parse_csv(p, MAPPINGS)
    p.write_text("Date,Description,Amount\n2026-01-01,X,abc\n", encoding="utf-8")
    with pytest.raises(ParseError):
        parse_csv(p, MAPPINGS)


def test_no_matching_mapping_is_parse_error(tmp_path: Path) -> None:
    p = tmp_path / "other.csv"
    p.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ParseError, match="no CSV mapping matches header"):
        parse_csv(p, MAPPINGS)


def test_bad_mappings_file_is_parse_error(tmp_path: Path) -> None:
    p = tmp_path / "m.yaml"
    p.write_text("mappings:\n  - name: x\n", encoding="utf-8")
    with pytest.raises(ParseError):
        load_mappings(p)
    p.write_text(
        "mappings:\n  - {name: x, match_header: [A], date_col: A, date_format: '%Y',"
        " description_col: A, account_name: n}\n",
        encoding="utf-8",
    )
    with pytest.raises(ParseError, match="amount_col"):
        load_mappings(p)


def test_oversized_file_is_parse_error(tmp_path: Path) -> None:
    p = tmp_path / "big.csv"
    with p.open("wb") as fh:
        fh.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(ParseError, match="too large"):
        detect_and_parse(p, MAPPINGS)


def test_ofx_and_qfx_fixtures() -> None:
    for name in ("sample.ofx", "sample.qfx"):
        stmt = detect_and_parse(FIX / name, MAPPINGS)
        assert stmt.account.id == "1234567890"
        assert stmt.account.currency == "USD"
        assert stmt.account.institution == "Example Bank"
        assert len(stmt.transactions) == 3
        assert _total(stmt) == Decimal("1902.70")
        assert stmt.transactions[0].description == "GREEN GROCER #12 CARD PURCHASE"
        assert stmt.transactions[1].description == "PAYROLL DEPOSIT"
        assert stmt.transactions[0].posted_on == date(2026, 1, 3)
        assert all(isinstance(t.amount, Decimal) for t in stmt.transactions)
        assert stmt.transactions[0].source_file == name


def test_ofx_missing_institution(tmp_path: Path) -> None:
    text = (FIX / "sample.ofx").read_text(encoding="ascii")
    start, end = text.index("<FI>"), text.index("</FI>") + len("</FI>")
    p = tmp_path / "nofi.ofx"
    p.write_text(text[:start] + text[end:], encoding="ascii")
    stmt = parse_ofx(p)
    assert stmt.account.institution is None
    assert len(stmt.transactions) == 3


def test_malformed_ofx_is_parse_error(tmp_path: Path) -> None:
    p = tmp_path / "junk.ofx"
    p.write_text("not an ofx file", encoding="ascii")
    with pytest.raises(ParseError):
        parse_ofx(p)


def test_unknown_extension_is_parse_error(tmp_path: Path) -> None:
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    with pytest.raises(ParseError, match="unsupported"):
        detect_and_parse(p, MAPPINGS)


def _write_csv(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "a.csv"
    p.write_text("Date,Description,Amount\n" + body, encoding="utf-8")
    return p


def test_csv_identical_rows_get_distinct_ids() -> None:
    stmt = parse_csv(FIX / "sample_duplicates.csv", MAPPINGS)
    assert len(stmt.transactions) == 3
    assert len({t.id for t in stmt.transactions}) == 3
    again = parse_csv(FIX / "sample_duplicates.csv", MAPPINGS)
    assert [t.id for t in again.transactions] == [t.id for t in stmt.transactions]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("-$1,234.56", "-1234.56"),
        ("($12.34)", "-12.34"),
        ("(-12.34)", "-12.34"),
        ("$ 5", "5"),
    ],
)
def test_strict_amounts_accepted(tmp_path: Path, raw: str, expected: str) -> None:
    p = _write_csv(tmp_path, f'2026-01-01,X,"{raw}"\n')
    assert parse_csv(p, MAPPINGS).transactions[0].amount == Decimal(expected)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "1e5", "(12.34", "12.34)", "12.34-", "--5"])
def test_strict_amounts_rejected(tmp_path: Path, raw: str) -> None:
    p = _write_csv(tmp_path, f'2026-01-01,X,"{raw}"\n')
    with pytest.raises(ParseError, match="invalid amount"):
        parse_csv(p, MAPPINGS)


def test_whitespace_amount_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = _write_csv(tmp_path, '2026-01-01,X,"  "\n')
    with caplog.at_level(logging.WARNING):
        assert parse_csv(p, MAPPINGS).transactions == ()
    assert "blank amount" in caplog.text


def test_csv_error_line_number_follows_reader(tmp_path: Path) -> None:
    p = _write_csv(tmp_path, '2026-01-01,"multi\nline",1.00\n2026-01-02,X,abc\n')
    with pytest.raises(ParseError, match="line 4"):
        parse_csv(p, MAPPINGS)


def test_malformed_mappings_yaml_is_parse_error(tmp_path: Path) -> None:
    p = tmp_path / "m.yaml"
    p.write_text("mappings: [unclosed\n", encoding="utf-8")
    with pytest.raises(ParseError, match="not valid YAML"):
        load_mappings(p)
