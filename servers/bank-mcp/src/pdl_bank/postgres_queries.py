"""SQL text and query builders for ``PostgresStore``.

Every statement is a constant; the only dynamic parts are placeholders and the group-by
key expression, which is looked up from a fixed table (never built from caller input).
Ordering uses the ``C`` collation so results match ``MemoryStore`` exactly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pdl_bank.store import GroupBy

UPSERT_ACCOUNT = (
    "INSERT INTO bank.accounts (id, name, institution, currency) VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, "
    "institution = EXCLUDED.institution, currency = EXCLUDED.currency"
)

INSERT_TRANSACTIONS = (
    "INSERT INTO bank.transactions "
    "(id, account_id, posted_on, amount, description, category, source_file) "
    "SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::numeric[], "
    "%s::text[], %s::text[], %s::text[]) "
    "ON CONFLICT (id) DO NOTHING"
)

ACCOUNTS = (
    "SELECT id, name, institution, currency FROM bank.accounts "
    'ORDER BY name COLLATE "C", id COLLATE "C"'
)

SPENDING_TRANSACTIONS = (
    "SELECT id, account_id, posted_on, amount, description, category, source_file "
    "FROM bank.transactions WHERE amount < 0"
)

RECORD_IMPORT = (
    "INSERT INTO bank.imports (file_hash, filename, imported_on, count) VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (file_hash) DO UPDATE SET filename = EXCLUDED.filename, "
    "imported_on = EXCLUDED.imported_on, count = EXCLUDED.count"
)

WAS_IMPORTED = "SELECT 1 FROM bank.imports WHERE file_hash = %s"

_GROUP_KEYS: dict[str, str] = {
    "category": "COALESCE(category, 'uncategorized')",
    "month": "to_char(posted_on, 'YYYY-MM')",
    "account": "account_id",
}

_SEARCH_SELECT = (
    "SELECT id, account_id, posted_on, amount, description, category, source_file "
    "FROM bank.transactions"
)
_SEARCH_ORDER = ' ORDER BY posted_on DESC, id COLLATE "C" ASC LIMIT %s'


def like_pattern(query: str) -> str:
    """Contains-pattern for ILIKE with backslash, percent and underscore matched literally."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_query(
    query: str | None,
    since: date | None,
    until: date | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
    limit: int,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if query:
        clauses.append("(description ILIKE %s OR category ILIKE %s)")
        params += [like_pattern(query)] * 2
    for clause, value in (
        ("posted_on >= %s", since),
        ("posted_on <= %s", until),
        ("amount >= %s", min_amount),
        ("amount <= %s", max_amount),
    ):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return _SEARCH_SELECT + where + _SEARCH_ORDER, [*params, max(limit, 1)]


def summary_query(group_by: GroupBy) -> str:
    key = _GROUP_KEYS[group_by]
    return (
        "SELECT grp, total, n FROM ("
        f"SELECT {key} AS grp, -SUM(amount) AS total, COUNT(*) AS n "
        "FROM bank.transactions WHERE amount < 0 AND posted_on >= %s AND posted_on <= %s "
        "GROUP BY 1) q "
        'ORDER BY total DESC, grp COLLATE "C" ASC'
    )
