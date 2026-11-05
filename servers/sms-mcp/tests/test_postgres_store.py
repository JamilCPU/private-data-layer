"""Postgres-specific behaviour (skipped unless TEST_PG_DSN is set)."""

from datetime import timedelta, timezone

from .conftest import make_message
from .store_cases import T0


def test_fulltext_matches_words_in_any_order(pg_store):
    pg_store.add([make_message(1, body="alpha beta gamma")])
    # "gamma alpha" is not a substring, so only the tsvector branch can match it.
    assert [m.id for m in pg_store.search("gamma alpha", None, None, 10)] == ["id1"]
    assert pg_store.search("gamma delta", None, None, 10) == []


def test_fulltext_finds_contact_name(pg_store):
    pg_store.add([make_message(1, contact="Dora Explorer"), make_message(2)])
    assert [m.id for m in pg_store.search("explorer dora", None, None, 10)] == ["id1"]


def test_two_char_query_uses_ilike(pg_store):
    pg_store.add([make_message(1, body="Go now"), make_message(2, body="other")])
    assert [m.id for m in pg_store.search("go", None, None, 10)] == ["id1"]
    assert [m.id for m in pg_store.search("  go ", None, None, 10)] == ["id1"]


def test_from_env_builds_store():
    from pdl_sms.postgres_store import PostgresStore

    assert isinstance(PostgresStore.from_env({"PGUSER": "u", "PGPASSWORD": "p"}), PostgresStore)


def test_sent_at_comes_back_utc(pg_store):
    sent = T0.astimezone(timezone(timedelta(hours=-5)))
    pg_store.add([make_message(1, sent_at=sent)])
    (got,) = pg_store.recent(None, 10)
    assert got.sent_at == T0 and got.sent_at.utcoffset() == timedelta(0)
