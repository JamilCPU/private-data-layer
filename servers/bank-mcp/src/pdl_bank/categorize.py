"""Rule-based transaction categoriser (first matching rule wins)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

UNCATEGORIZED = "uncategorized"


class RulesError(ValueError):
    """Raised when the rules file is malformed."""


@dataclass(frozen=True)
class Rule:
    category: str
    pattern: re.Pattern[str]


def _build_rule(index: int, entry: object) -> Rule:
    if not isinstance(entry, dict):
        raise RulesError(f"rule #{index}: expected a mapping")
    category, match = entry.get("category"), entry.get("match")
    if not isinstance(category, str) or not category:
        raise RulesError(f"rule #{index}: missing 'category'")
    if not isinstance(match, str) or not match:
        raise RulesError(f"rule #{index} ({category}): missing 'match'")
    try:
        return Rule(category, re.compile(match, re.IGNORECASE))
    except re.error as exc:
        raise RulesError(f"rule #{index} ({category}): invalid regex {match!r}: {exc}") from exc


def load_rules(path: Path) -> tuple[Rule, ...]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RulesError(f"rules file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise RulesError("rules file must be a mapping with a 'rules' list")
    return tuple(_build_rule(i, e) for i, e in enumerate(data["rules"], start=1))


def categorize(description: str, rules: Iterable[Rule]) -> str:
    for rule in rules:
        if rule.pattern.search(description):
            return rule.category
    return UNCATEGORIZED
