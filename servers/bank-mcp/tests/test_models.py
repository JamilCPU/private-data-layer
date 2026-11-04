from datetime import date
from decimal import Decimal

from pdl_bank.models import Account, Transaction, transaction_id


def test_id_determinism_and_normalisation() -> None:
    d = date(2026, 1, 5)
    a = transaction_id("acc", d, Decimal("-12.5"), "Green  Grocer ")
    b = transaction_id("acc", d, Decimal("-12.50"), "  green grocer")
    assert a == b
    assert len(a) == 32
    assert a != transaction_id("acc", d, Decimal("-12.51"), "green grocer")
    assert a != transaction_id("other", d, Decimal("-12.50"), "green grocer")
    assert a != transaction_id("acc", date(2026, 1, 6), Decimal("-12.50"), "green grocer")


def test_amounts_are_decimal() -> None:
    tx = Transaction(
        id="x",
        account_id="acc",
        posted_on=date(2026, 1, 5),
        amount=Decimal("0.10") + Decimal("0.20"),
        description="d",
        category=None,
        source_file="f.csv",
    )
    assert isinstance(tx.amount, Decimal)
    assert tx.amount == Decimal("0.30")
    assert Account(id="a", name="n", institution=None).currency == "USD"
