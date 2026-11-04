"""Postgres-backed Store (schema ``bank``). One short-lived connection per call."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from pdl_bank import postgres_queries as q
from pdl_bank.models import Account, Transaction
from pdl_bank.recurring import recurring_charges
from pdl_bank.store import GroupBy, RecurringCharge, SummaryRow


def _tx(row: tuple[Any, ...]) -> Transaction:
    return Transaction(*row)


class PostgresStore:
    def __init__(
        self, dsn: str, *, connect: Callable[[], psycopg.Connection[Any]] | None = None
    ) -> None:
        self._connect: Callable[[], psycopg.Connection[Any]] = connect or (
            lambda: psycopg.connect(dsn)
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PostgresStore:
        from pdl_bank.store_factory import dsn_from_env

        return cls(dsn_from_env(env))

    def upsert_account(self, a: Account) -> None:
        with self._connect() as conn:
            conn.execute(q.UPSERT_ACCOUNT, (a.id, a.name, a.institution, a.currency))

    def upsert_transactions(self, txs: Sequence[Transaction]) -> int:
        if not txs:
            return 0
        columns = [
            [t.id for t in txs],
            [t.account_id for t in txs],
            [t.posted_on for t in txs],
            [t.amount for t in txs],
            [t.description for t in txs],
            [t.category for t in txs],
            [t.source_file for t in txs],
        ]
        with self._connect() as conn:
            return conn.execute(q.INSERT_TRANSACTIONS, columns).rowcount

    def accounts(self) -> list[Account]:
        with self._connect() as conn:
            rows = conn.execute(q.ACCOUNTS).fetchall()
        return [Account(*row) for row in rows]

    def search(
        self,
        query: str | None,
        since: date | None,
        until: date | None,
        min_amount: Decimal | None,
        max_amount: Decimal | None,
        limit: int,
    ) -> list[Transaction]:
        text, params = q.search_query(query, since, until, min_amount, max_amount, limit)
        with self._connect() as conn:
            rows = conn.execute(text, params).fetchall()  # type: ignore[arg-type]
        return [_tx(row) for row in rows]

    def summary(self, since: date, until: date, group_by: GroupBy) -> list[SummaryRow]:
        text = q.summary_query(group_by)
        with self._connect() as conn:
            rows = conn.execute(text, (since, until)).fetchall()  # type: ignore[arg-type]
        return [SummaryRow(key, total, int(count)) for key, total, count in rows]

    def recurring(self, min_occurrences: int = 3) -> list[RecurringCharge]:
        with self._connect() as conn:
            rows = conn.execute(q.SPENDING_TRANSACTIONS).fetchall()  # type: ignore[arg-type]
        return recurring_charges([_tx(row) for row in rows], min_occurrences)

    def record_import(self, file_hash: str, filename: str, imported_on: date, count: int) -> None:
        with self._connect() as conn:
            conn.execute(q.RECORD_IMPORT, (file_hash, filename, imported_on, count))  # type: ignore[arg-type]

    def was_imported(self, file_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(q.WAS_IMPORTED, (file_hash,)).fetchone()  # type: ignore[arg-type]
        return row is not None
