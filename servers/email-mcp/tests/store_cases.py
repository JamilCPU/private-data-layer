from datetime import UTC, datetime, timedelta, timezone

from pdl_email.memory_store import MemoryStore
from pdl_email.models import Message, message_id
from pdl_email.store import SyncState

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def make(uid: int, *, subject="s", frm="a@x.test", name=None, body="b", sent=T0) -> Message:
    return Message(
        id=message_id("INBOX", 1, uid),
        uid=uid,
        uidvalidity=1,
        mailbox="INBOX",
        from_addr=frm,
        from_name=name,
        to_addrs=("me@x.test",),
        subject=subject,
        sent_at=sent,
        snippet=body[:10],
        body_text=body,
        has_attachments=False,
        message_id_header=None,
    )


def test_round_trip_and_new_count_without_overwrite():
    store = MemoryStore()
    m = make(1, subject="orig")
    assert store.upsert_messages([m, make(2)]) == 2
    assert store.upsert_messages([make(1, subject="changed"), make(3)]) == 1
    assert store.get(m.id) == m
    assert store.get("nope") is None


def test_search_each_field_case_insensitive():
    store = MemoryStore()
    store.upsert_messages(
        [
            make(1, subject="Invoice March"),
            make(2, frm="Billing@Shop.test"),
            make(3, name="Carol Danvers"),
            make(4, body="your Parcel shipped"),
            make(5),
        ]
    )

    def uids(q):
        return sorted(m.uid for m in store.search(q, None, 10))

    assert uids("invoice") == [1]
    assert uids("BILLING@shop") == [2]
    assert uids("danvers") == [3]
    assert uids("parcel") == [4]
    assert uids(None) == [1, 2, 3, 4, 5]
    assert uids("") == [1, 2, 3, 4, 5]
    assert uids("zzz") == []


def test_since_aware_and_naive():
    store = MemoryStore()
    store.upsert_messages([make(1, sent=T0 - timedelta(days=5)), make(2, sent=T0)])
    assert [m.uid for m in store.recent(T0, 10)] == [2]  # >= is inclusive
    assert [m.uid for m in store.recent(T0.replace(tzinfo=None), 10)] == [2]
    plus2 = T0.astimezone(timezone(timedelta(hours=2)))
    assert [m.uid for m in store.recent(plus2, 10)] == [2]
    assert len(store.search(None, T0 - timedelta(days=9), 10)) == 2


def test_ordering_ties_and_limit_clamp():
    store = MemoryStore()
    msgs = [make(i, sent=T0) for i in range(1, 4)] + [make(9, sent=T0 + timedelta(hours=1))]
    store.upsert_messages(msgs)
    out = store.recent(None, 10)
    assert out[0].uid == 9
    assert [m.id for m in out[1:]] == sorted(m.id for m in out[1:])
    assert len(store.recent(None, 2)) == 2
    assert len(store.recent(None, 0)) == 1
    assert len(store.search(None, None, -5)) == 1


def test_sync_state():
    store = MemoryStore()
    assert store.sync_state("INBOX") is None
    store.set_sync_state("INBOX", SyncState(5, 10))
    assert store.sync_state("INBOX") == SyncState(5, 10)
    assert store.sync_state("Other") is None
