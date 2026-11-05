"""Apply plain ``*.sql`` migrations once each, tracked in ``<schema>.schema_migrations``."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql

# servers/sms-store/migrations, resolved from src/pdl_sms_store/ (and /app/migrations in the image).
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# Serialises concurrent migration runs of this package (distinct from the bank package's key).
_LOCK_KEY = 0x70646C_736D73  # "pdl" + "sms"


def _ensure_tracking_table(conn: psycopg.Connection, schema: str) -> None:
    # CREATE SCHEMA IF NOT EXISTS demands CREATE on the database even when the schema exists,
    # which a non-superuser owner role lacks; create it only when it is really missing.
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
    conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR, *, schema: str = "sms"
) -> list[str]:
    """Run each unapplied migration in its own transaction; return the names applied.

    Every transaction first takes a transaction-scoped advisory lock and re-reads the ledger, so
    two concurrent jobs cannot apply the same file twice. All statements, including the first
    ledger read, run inside ``conn.transaction()`` so a non-autocommit connection never turns the
    per-file transactions into savepoints of one long transaction.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        _ensure_tracking_table(conn, schema)
        done = _applied(conn, schema)
    applied: list[str] = []
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
        applied.append(path.name)
    return applied
