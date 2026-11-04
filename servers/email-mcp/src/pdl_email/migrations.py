"""Apply plain ``*.sql`` migrations once each, tracked in ``<schema>.schema_migrations``."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql

# servers/email-mcp/migrations, resolved from src/pdl_email/ (and /app/migrations in the image).
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def apply_migrations(
    conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR, *, schema: str = "email"
) -> list[str]:
    """Run each unapplied migration in its own transaction; return the names applied."""
    table = sql.Identifier(schema, "schema_migrations")
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} ("
                "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            ).format(table)
        )
    done = {r[0] for r in conn.execute(sql.SQL("SELECT name FROM {}").format(table)).fetchall()}
    applied: list[str] = []
    for path in sorted(Path(migrations_dir).glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
            conn.execute(sql.SQL("INSERT INTO {} (name) VALUES (%s)").format(table), (path.name,))
        applied.append(path.name)
    return applied
