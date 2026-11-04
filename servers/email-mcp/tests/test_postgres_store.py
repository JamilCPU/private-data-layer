"""Postgres-specific behaviour (skipped unless TEST_PG_DSN is set)."""

from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from pdl_email.migrations import MIGRATIONS_DIR, apply_migrations

from .store_cases import T0, make


def test_migrations_idempotent_and_recorded(pg_dsn):
    with psycopg.connect(pg_dsn) as conn:
        assert apply_migrations(conn) == []
        names = [r[0] for r in conn.execute("SELECT name FROM email.schema_migrations")]
    assert names == ["001_email.sql"]


def test_migrations_apply_in_order_in_a_scratch_schema(pg_dsn, tmp_path):
    (tmp_path / "002_b.sql").write_text("CREATE TABLE scratch_mig.b (x int);")
    (tmp_path / "001_a.sql").write_text("CREATE TABLE scratch_mig.a (x int);")
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS scratch_mig CASCADE")
        try:
            assert apply_migrations(conn, tmp_path, schema="scratch_mig") == [
                "001_a.sql",
                "002_b.sql",
            ]
            assert apply_migrations(conn, tmp_path, schema="scratch_mig") == []
        finally:
            conn.execute("DROP SCHEMA IF EXISTS scratch_mig CASCADE")


def test_failed_migration_rolls_back(pg_dsn, tmp_path):
    (tmp_path / "001_bad.sql").write_text("CREATE TABLE scratch_bad.t (x int); SELECT 1/0;")
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS scratch_bad CASCADE")
        try:
            with pytest.raises(psycopg.errors.DivisionByZero):
                apply_migrations(conn, tmp_path, schema="scratch_bad")
            assert conn.execute("SELECT to_regclass('scratch_bad.t')").fetchone() == (None,)
            assert conn.execute(
                "SELECT count(*) FROM scratch_bad.schema_migrations"
            ).fetchone() == (0,)
        finally:
            conn.execute("DROP SCHEMA IF EXISTS scratch_bad CASCADE")


def test_fulltext_finds_word_in_body_and_subject(pg_store):
    pg_store.upsert_messages(
        [
            make(1, subject="Quarterly report", body="nothing"),
            make(2, subject="x", body="the landlord wrote about heating"),
            make(3, subject="y", body="unrelated"),
        ]
    )
    assert [m.uid for m in pg_store.search("report", None, 10)] == [1]
    assert [m.uid for m in pg_store.search("heating landlord", None, 10)] == [2]


def test_fulltext_matches_words_in_any_order(pg_store):
    pg_store.upsert_messages([make(1, body="alpha beta gamma")])
    # The phrase "gamma alpha" is not a substring, so only the tsvector branch can match it.
    assert [m.uid for m in pg_store.search("gamma alpha", None, 10)] == [1]
    assert pg_store.search("gamma delta", None, 10) == []


def test_two_char_query_uses_ilike(pg_store):
    pg_store.upsert_messages([make(1, subject="Go Live"), make(2, subject="other")])
    assert [m.uid for m in pg_store.search("go", None, 10)] == [1]
    assert [m.uid for m in pg_store.search("  go ", None, 10)] == [1]


def test_migrations_dir_resolves_to_package_migrations():
    assert MIGRATIONS_DIR == Path(__file__).resolve().parents[1] / "migrations"
    assert (MIGRATIONS_DIR / "001_email.sql").is_file()


def test_unique_mailbox_uid_index(pg_store):
    pg_store.upsert_messages([make(1)])
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_store.upsert_messages([replace(make(1), id="different-id")])


def test_sent_at_comes_back_utc(pg_store):
    sent = T0.astimezone(timezone(timedelta(hours=-5)))
    pg_store.upsert_messages([replace(make(1), sent_at=sent)])
    got = pg_store.get(make(1).id)
    assert got is not None
    assert got.sent_at == T0 and got.sent_at.utcoffset() == timedelta(0)
