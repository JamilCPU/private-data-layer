"""Apply ``migrations/*.sql`` in order, tracked in ``<schema>.schema_migrations``."""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

# <repo>/servers/bank-mcp/migrations; in the image this is /app/migrations.
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"

# Serialises concurrent migration runs of this package (distinct from the email package's key).
_LOCK_KEY = 0x70646C_62616E  # "pdl" + "ban"


def _ensure_tracking_table(conn: psycopg.Connection, schema: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone()
    if exists is None:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        ).format(sql.Identifier(schema))
    )


def _applied(conn: psycopg.Connection, schema: str) -> set[str]:
    query = sql.SQL("SELECT name FROM {}.schema_migrations").format(sql.Identifier(schema))
    return {row[0] for row in conn.execute(query).fetchall()}


def apply_migrations(
    conn: psycopg.Connection, migrations_dir: Path, *, schema: str = "bank"
) -> list[str]:
    """Run each not-yet-applied ``*.sql`` file in its own transaction; return names applied.

    Every transaction takes a transaction-scoped advisory lock and re-reads the ledger, so two
    concurrent jobs cannot apply the same file twice.
    """
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        _ensure_tracking_table(conn, schema)
        done = _applied(conn, schema)
    for path in sorted(Path(migrations_dir).glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            if path.name in _applied(conn, schema):
                continue
            conn.execute(sql.SQL(path.read_text(encoding="utf-8")))  # type: ignore[arg-type]
            conn.execute(
                sql.SQL("INSERT INTO {}.schema_migrations (name) VALUES (%s)").format(
                    sql.Identifier(schema)
                ),
                (path.name,),
            )
        log.info("applied migration %s", path.name)
        applied_now.append(path.name)
    return applied_now
