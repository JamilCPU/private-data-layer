"""Postgres-specific behaviour (skipped unless TEST_PG_DSN is set)."""

from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from pdl_sms_store.migrations import MIGRATIONS_DIR, apply_migrations

from .store_cases import make


def test_migrations_idempotent_and_recorded(pg_dsn):
    with psycopg.connect(pg_dsn) as conn:
        assert apply_migrations(conn) == []
        names = [r[0] for r in conn.execute("SELECT name FROM sms.schema_migrations")]
    assert names == ["001_sms.sql"]


def test_migrations_dir_resolves_to_package_migrations():
    assert MIGRATIONS_DIR == Path(__file__).resolve().parents[1] / "migrations"
    assert (MIGRATIONS_DIR / "001_sms.sql").is_file()


def test_failed_migration_rolls_back(pg_dsn, tmp_path):
    (tmp_path / "001_bad.sql").write_text("CREATE TABLE scratch_bad.t (x int); SELECT 1/0;")
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS scratch_bad CASCADE")
        try:
            with pytest.raises(psycopg.errors.DivisionByZero):
                apply_migrations(conn, tmp_path, schema="scratch_bad")
            assert conn.execute("SELECT to_regclass('scratch_bad.t')").fetchone() == (None,)
        finally:
            conn.execute("DROP SCHEMA IF EXISTS scratch_bad CASCADE")


def test_direction_check_constraint(pg_dsn):
    insert = (
        "INSERT INTO sms.messages (id, device, provider_id, address, direction, body, "
        "sent_at, received_at) VALUES ('x','d','p','a','sideways','b',now(),now())"
    )
    with (
        psycopg.connect(pg_dsn, autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute(insert)


def test_unique_device_provider_sent_at(pg_store):
    pg_store.upsert_messages([make(1)])
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg_store.upsert_messages([replace(make(1), id="different-id")])


def test_fulltext_column_covers_contact_address_body(pg_store, pg_dsn):
    pg_store.upsert_messages([make(1, body="parcel arrives", contact="Alex Example")])
    with psycopg.connect(pg_dsn) as conn:
        hits = [
            conn.execute(
                "SELECT count(*) FROM sms.messages WHERE tsv @@ plainto_tsquery('simple', %s)",
                (q,),
            ).fetchone()[0]
            for q in ("parcel", "alex", "15550100")
        ]
        # A query with the leading "+" lexes as a signed number, so callers normalise it the
        # same way the column does before matching.
        plus = conn.execute(
            "SELECT count(*) FROM sms.messages WHERE tsv @@ plainto_tsquery("
            "'simple', regexp_replace(%s, '[^[:alnum:]]+', ' ', 'g'))",
            ("+15550100",),
        ).fetchone()[0]
    assert hits == [1, 1, 1]
    assert plus == 1


def test_reader_role_gets_select_when_it_exists(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sms_reader') "
            "THEN CREATE ROLE sms_reader NOLOGIN; END IF; END $$"
        )
        conn.execute("DROP SCHEMA IF EXISTS sms CASCADE")
        apply_migrations(conn)
        assert conn.execute(
            "SELECT has_table_privilege('sms_reader', 'sms.messages', 'SELECT'), "
            "has_table_privilege('sms_reader', 'sms.messages', 'INSERT')"
        ).fetchone() == (True, False)
        conn.execute("CREATE TABLE sms.later (x int)")
        assert conn.execute(
            "SELECT has_table_privilege('sms_reader', 'sms.later', 'SELECT')"
        ).fetchone() == (True,)
        conn.execute("DROP TABLE sms.later")


def test_migration_applies_without_reader_role(pg_dsn, tmp_path):
    text = (MIGRATIONS_DIR / "001_sms.sql").read_text(encoding="utf-8")
    text = text.replace("sms_reader", "sms_reader_absent_role").replace("sms", "scratch_nr")
    (tmp_path / "001_sms.sql").write_text(text)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS scratch_nr CASCADE")
        try:
            assert apply_migrations(conn, tmp_path, schema="scratch_nr") == ["001_sms.sql"]
        finally:
            conn.execute("DROP SCHEMA IF EXISTS scratch_nr CASCADE")
