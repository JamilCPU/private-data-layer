"""Behavioural cases every Store implementation must satisfy."""

from datetime import UTC, datetime, timedelta

from pdl_sms_store.models import Message, message_id

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def make(n: int, *, device="pixel", body="hello", contact=None) -> Message:
    return Message(
        id=message_id(device, f"p{n}", 1_000 + n),
        device=device,
        provider_id=f"p{n}",
        thread_id="t1",
        address="+15550100",
        contact=contact,
        direction="in",
        body=body,
        sent_at=T0 + timedelta(seconds=n),
        received_at=T0 + timedelta(minutes=1),
    )


def case_upsert_is_idempotent_and_counts_new(store):
    assert store.upsert_messages([make(1), make(2)]) == 2
    assert store.upsert_messages([make(1, body="changed"), make(3)]) == 1
    assert store.upsert_messages([]) == 0
    assert store.count() == 3


def case_in_batch_duplicates_count_once(store):
    assert store.upsert_messages([make(1, body="first"), make(1, body="second")]) == 1
    assert store.count() == 1


def case_device_seen_round_trip(store):
    store.record_device_seen("pixel", T0, "p1")
    assert store.device_seen("pixel") == (T0, "p1")
    later = T0 + timedelta(hours=1)
    store.record_device_seen("pixel", later, None)
    assert store.device_seen("pixel") == (later, None)
    assert store.device_seen("other") is None


def case_count_empty(store):
    assert store.count() == 0


CASES = [
    case_upsert_is_idempotent_and_counts_new,
    case_in_batch_duplicates_count_once,
    case_device_seen_round_trip,
    case_count_empty,
]
