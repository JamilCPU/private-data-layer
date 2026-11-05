"""Tests for the SQLite memory store."""

import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pdl_assistant.memory import Conversation, MemoryStore, StoredMessage


@pytest.fixture
def store(tmp_path: Path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


def test_wal_enabled(store: MemoryStore, tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "mem.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_creates_parent_directory(tmp_path: Path):
    s = MemoryStore(tmp_path / "nested" / "dir" / "m.db")
    s.close()
    assert (tmp_path / "nested" / "dir" / "m.db").exists()


def test_conversation_round_trip(store: MemoryStore):
    cid = store.new_conversation("Hello")
    other = store.new_conversation()
    convs = store.list_conversations()
    assert {c.id for c in convs} == {cid, other}
    by_id = {c.id: c for c in convs}
    assert by_id[cid].title == "Hello"
    assert by_id[other].title is None
    assert isinstance(by_id[cid], Conversation)
    parsed = datetime.fromisoformat(by_id[cid].created_at)
    assert parsed.utcoffset().total_seconds() == 0


def test_list_conversations_newest_first_and_limit(store: MemoryStore):
    ids = [store.new_conversation(f"c{i}") for i in range(5)]
    listed = store.list_conversations(limit=3)
    assert [c.id for c in listed] == ids[::-1][:3]


def test_messages_round_trip(store: MemoryStore):
    cid = store.new_conversation()
    m1 = store.add_message(cid, "user", "hi")
    m2 = store.add_message(cid, "tool", '{"a":1}', tool_name="search")
    msgs = store.recent_messages(cid)
    assert [m.id for m in msgs] == [m1, m2]
    assert isinstance(msgs[0], StoredMessage)
    assert (msgs[0].role, msgs[0].content, msgs[0].tool_name) == ("user", "hi", None)
    assert msgs[1].tool_name == "search"
    assert msgs[1].conversation_id == cid


def test_recent_messages_limit_is_last_n_chronological(store: MemoryStore):
    cid = store.new_conversation()
    ids = [store.add_message(cid, "user", f"m{i}") for i in range(10)]
    msgs = store.recent_messages(cid, limit=4)
    assert [m.id for m in msgs] == ids[-4:]
    assert [m.content for m in msgs] == ["m6", "m7", "m8", "m9"]


def test_messages_scoped_to_conversation(store: MemoryStore):
    a, b = store.new_conversation(), store.new_conversation()
    store.add_message(a, "user", "for a")
    assert store.recent_messages(b) == []


def test_records_are_frozen(store: MemoryStore):
    cid = store.new_conversation("t")
    conv = store.list_conversations()[0]
    with pytest.raises(AttributeError):
        conv.title = "x"  # type: ignore[misc]
    store.add_message(cid, "user", "hi")
    with pytest.raises(AttributeError):
        store.recent_messages(cid)[0].content = "x"  # type: ignore[misc]


def test_preference_upsert(store: MemoryStore):
    assert store.get_preference("tone") is None
    store.set_preference("tone", "brief")
    store.set_preference("tone", "chatty")
    assert store.get_preference("tone") == "chatty"


def test_sessions(store: MemoryStore):
    sid = store.create_session()
    assert len(sid) >= 32
    assert sid != store.create_session()
    assert store.session_valid(sid)
    store.revoke_session(sid)
    assert not store.session_valid(sid)
    assert not store.session_valid("unknown")
    store.revoke_session("unknown")  # no error


def test_persists_across_reopen(tmp_path: Path):
    path = tmp_path / "p.db"
    first = MemoryStore(path)
    cid = first.new_conversation("keep")
    first.add_message(cid, "user", "x")
    sid = first.create_session()
    first.close()
    second = MemoryStore(path)
    assert second.list_conversations()[0].title == "keep"
    assert second.recent_messages(cid)[0].content == "x"
    assert second.session_valid(sid)
    second.close()


def test_get_conversation(store: MemoryStore):
    cid = store.new_conversation("hello")
    found = store.get_conversation(cid)
    assert found is not None
    assert (found.id, found.title) == (cid, "hello")
    assert store.get_conversation(cid + 1) is None


def test_last_sent_briefing_at_coerces_naive_to_utc(store: MemoryStore):
    from datetime import UTC

    store.record_briefing("2026-11-03T07:00:00", "sent", "t", None)
    got = store.last_sent_briefing_at()
    assert got is not None and got.tzinfo is UTC
    assert got.hour == 7


def _ts(day: int) -> str:
    return datetime(2026, 11, day, 12, tzinfo=UTC).isoformat()


def test_add_flag_dedupes_on_check_and_key(store: MemoryStore):
    assert store.add_flag("big", "tx1", "Large charge", _ts(1)) is True
    assert store.add_flag("big", "tx1", "Large charge again", _ts(2)) is False
    assert store.add_flag("big", "tx2", "Another", _ts(2)) is True
    assert store.add_flag("other", "tx1", "Same key, other check", _ts(2)) is True


def test_unnotified_flags_oldest_first_and_mark_notified(store: MemoryStore):
    store.add_flag("c", "a", "first", _ts(1))
    store.add_flag("c", "b", "second", _ts(2))
    store.add_flag("c", "c", "third", _ts(3))
    pending = store.unnotified_flags()
    assert [f.key for f in pending] == ["a", "b", "c"]
    assert all(f.notified_at is None for f in pending)
    store.mark_flags_notified([pending[0].id, pending[2].id], _ts(4))
    assert [f.key for f in store.unnotified_flags()] == ["b"]
    done = store.flags_since(datetime(2026, 11, 1, tzinfo=UTC))
    assert {f.key: f.notified_at for f in done}["a"] == _ts(4)


def test_mark_flags_notified_empty_is_noop(store: MemoryStore):
    store.add_flag("c", "a", "first", _ts(1))
    store.mark_flags_notified([], _ts(2))
    assert len(store.unnotified_flags()) == 1


def test_flags_since_boundary_and_order(store: MemoryStore):
    store.add_flag("c", "old", "old", _ts(1))
    store.add_flag("c", "edge", "edge", _ts(2))
    store.add_flag("c", "new", "new", _ts(3))
    got = store.flags_since(datetime(2026, 11, 2, 12, tzinfo=UTC))
    assert [f.key for f in got] == ["edge", "new"]


def test_flags_since_naive_is_utc(store: MemoryStore):
    store.add_flag("c", "edge", "edge", _ts(2))
    assert len(store.flags_since(datetime(2026, 11, 2, 12))) == 1


def test_stored_flag_is_frozen(store: MemoryStore):
    store.add_flag("c", "a", "s", _ts(1))
    flag = store.unnotified_flags()[0]
    with pytest.raises(FrozenInstanceError):
        flag.summary = "x"  # type: ignore[misc]


def test_existing_database_gains_flags_table(tmp_path: Path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.commit()
    conn.close()
    s = MemoryStore(path)
    try:
        assert s.add_flag("c", "k", "s", _ts(1)) is True
    finally:
        s.close()
