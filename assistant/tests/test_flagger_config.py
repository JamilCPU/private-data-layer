"""Tests for the flagger check configuration loader."""

from decimal import Decimal
from pathlib import Path

import pytest

from pdl_assistant.flagger_config import (
    EmailFrom,
    FlaggerConfigError,
    RecurringChange,
    TransactionOver,
    load_checks,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "flagger.example.yaml"


def _load(tmp_path: Path, text: str):
    path = tmp_path / "flagger.yaml"
    path.write_text(text, encoding="utf-8")
    return load_checks(path)


def test_example_file_loads() -> None:
    checks = load_checks(EXAMPLE)
    assert checks == (
        TransactionOver("large-transaction", Decimal(500), "spend"),
        RecurringChange("recurring-change", Decimal(20)),
        EmailFrom("watched-sender", ("billing@example.com",)),
    )


def test_numbers_and_any_direction(tmp_path: Path) -> None:
    checks = _load(
        tmp_path,
        "checks:\n  - {name: a, type: transaction_over, amount: 99.5, direction: any}\n"
        "  - {name: b, type: recurring_change, percent: 1000}\n",
    )
    assert checks[0] == TransactionOver("a", Decimal("99.5"), "any")
    assert checks[1] == RecurringChange("b", Decimal(1000))


def test_senders_lowercased(tmp_path: Path) -> None:
    checks = _load(
        tmp_path, "checks:\n  - {name: s, type: email_from, senders: ['Bill@Example.COM ']}\n"
    )
    assert checks[0] == EmailFrom("s", ("bill@example.com",))


def test_missing_file_names_path(tmp_path: Path) -> None:
    with pytest.raises(FlaggerConfigError, match="nope.yaml"):
        load_checks(tmp_path / "nope.yaml")


def _tx(**kw: object) -> str:
    base = {"name": "a", "type": "transaction_over", "amount": "5"} | kw
    return "checks:\n  - " + str(base).replace("'", '"').replace("True", "true") + "\n"


def _many(n: int) -> str:
    rows = "".join(f'  - {{name: c{i}, type: recurring_change, percent: "5"}}\n' for i in range(n))
    return "checks:\n" + rows


_SENDERS_51 = ",".join(f"s{i}@x.io" for i in range(51))

BAD = [
    pytest.param(_tx(amount="0"), "finite number > 0", id="amount-zero"),
    pytest.param(_tx(amount="-3"), "finite number > 0", id="amount-negative"),
    pytest.param(_tx(amount="NaN"), "finite number > 0", id="amount-nan"),
    pytest.param(_tx(amount="Infinity"), "finite number > 0", id="amount-inf"),
    pytest.param(_tx(amount="abc"), "must be a number", id="amount-garbage"),
    pytest.param(_tx(amount=True), "must be a number", id="amount-bool"),
    pytest.param(_tx(direction="in"), "direction must be one of", id="direction"),
    pytest.param(_tx(name="Bad Name"), "name must match", id="name-bad"),
    pytest.param(_tx(name=""), "name must match", id="name-empty"),
    pytest.param(_tx(name="x" * 41), "name must match", id="name-long"),
    pytest.param(_tx(type="nope"), "unknown type", id="unknown-type"),
    pytest.param(_tx(extra="1"), "unknown key", id="unknown-key"),
    pytest.param(_tx(percent="5"), "unknown key", id="key-of-other-type"),
    pytest.param(
        'checks:\n  - {name: a, type: recurring_change, percent: "0"}\n',
        "finite number > 0",
        id="percent-zero",
    ),
    pytest.param(
        'checks:\n  - {name: a, type: recurring_change, percent: "1001"}\n',
        "must be <= 1000",
        id="percent-1001",
    ),
    pytest.param(
        "checks:\n  - {name: a, type: email_from, senders: []}\n",
        "senders must be a list",
        id="senders-empty",
    ),
    pytest.param(
        "checks:\n  - {name: a, type: email_from, senders: ['  ']}\n",
        "non-blank strings",
        id="sender-blank",
    ),
    pytest.param(
        "checks:\n  - {name: a, type: email_from, senders: x}\n",
        "senders must be a list",
        id="senders-str",
    ),
    pytest.param(
        "checks:\n  - {name: a, type: email_from, senders: [" + _SENDERS_51 + "]}\n",
        "senders must be a list",
        id="senders-51",
    ),
    pytest.param(
        'checks:\n  - {name: a, type: recurring_change, percent: "5"}\n'
        '  - {name: a, type: recurring_change, percent: "6"}\n',
        "duplicate name",
        id="duplicate-names",
    ),
    pytest.param("checks: []\n", "non-empty list", id="empty-checks"),
    pytest.param("other: 1\n", "only a 'checks' key", id="no-checks"),
    pytest.param(_many(21), "at most 20", id="21-checks"),
    pytest.param("- a\n- b\n", "only a 'checks' key", id="not-a-mapping"),
    pytest.param("checks:\n  - just a string\n", "expected a mapping", id="check-not-mapping"),
    pytest.param("checks: [: bad\n", "cannot read flagger checks", id="yaml-syntax"),
]


@pytest.mark.parametrize(("text", "reason"), BAD)
def test_rejects(tmp_path: Path, text: str, reason: str) -> None:
    with pytest.raises(FlaggerConfigError, match="flagger.yaml") as caught:
        _load(tmp_path, text)
    assert reason in str(caught.value)


def test_twenty_checks_allowed(tmp_path: Path) -> None:
    assert len(_load(tmp_path, _many(20))) == 20


def test_error_names_check(tmp_path: Path) -> None:
    with pytest.raises(FlaggerConfigError, match="'a'"):
        _load(tmp_path, _tx(amount="0"))
