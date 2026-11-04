from pathlib import Path

import pytest

from pdl_bank.categorize import Rule, RulesError, categorize, load_rules

EXAMPLE = Path(__file__).parent.parent / "rules.example.yaml"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "rules.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_rule_order_first_match_wins(tmp_path: Path) -> None:
    rules = load_rules(
        _write(
            tmp_path,
            "rules:\n  - {category: a, match: 'shop'}\n  - {category: b, match: 'shop'}\n",
        )
    )
    assert isinstance(rules[0], Rule)
    assert categorize("Corner SHOP", rules) == "a"


def test_no_match_is_uncategorized(tmp_path: Path) -> None:
    rules = load_rules(_write(tmp_path, "rules:\n  - {category: a, match: 'shop'}\n"))
    assert categorize("something else", rules) == "uncategorized"
    assert categorize("anything", ()) == "uncategorized"


@pytest.mark.parametrize(
    "body",
    [
        "rules:\n  - {category: a, match: '('}\n",
        "rules:\n  - {category: a}\n",
        "rules:\n  - {match: 'x'}\n",
        "rules: nope\n",
        "- not a mapping\n",
    ],
)
def test_bad_regex_is_rules_error(tmp_path: Path, body: str) -> None:
    with pytest.raises(RulesError):
        load_rules(_write(tmp_path, body))


def test_example_rules_load() -> None:
    rules = load_rules(EXAMPLE)
    assert len(rules) >= 8
    assert categorize("STREAMFLIX monthly", rules) == "subscriptions"
    assert categorize("GREEN GROCER #12", rules) == "groceries"


def test_malformed_rules_yaml_is_rules_error(tmp_path: Path) -> None:
    with pytest.raises(RulesError, match="not valid YAML"):
        load_rules(_write(tmp_path, "rules: [unclosed\n"))


def test_example_rent_rule_is_word_bounded() -> None:
    rules = load_rules(EXAMPLE)
    assert categorize("MONTHLY RENT", rules) == "rent"
    assert categorize("current account fee", rules) != "rent"
    assert categorize("parent teacher shop", rules) != "rent"
