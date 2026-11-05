"""Behavioural cases every Store implementation must satisfy.

Each ``case_*`` function takes an empty store with an ``add`` method; ``test_store_contract``
runs them against the MemoryStore and (when TEST_PG_DSN is set) the PostgresStore.
"""

from datetime import UTC, datetime, timedelta, timezone

from .conftest import make_message

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _msg(n: int, **kw):
    kw.setdefault("sent_at", T0)
    return make_message(n, **kw)


def case_search_each_field_case_insensitive(store):
    store.add(
        [
            _msg(1, body="your Parcel shipped"),
            _msg(2, contact="Carol Danvers"),
            _msg(3, address="+15559876"),
            _msg(4),
        ]
    )

    def ids(q):
        return sorted(m.id for m in store.search(q, None, None, 10))

    assert ids("PARCEL") == ["id1"]
    assert ids("danvers") == ["id2"]
    assert ids("98") == ["id3"]
    assert ids("zzz") == []


def case_blank_query_behaves_like_recent(store):
    store.add([_msg(1), _msg(2, contact="Zed")])
    assert len(store.search(None, None, None, 10)) == 2
    assert len(store.search("", None, None, 10)) == 2
    assert len(store.search("   ", None, None, 10)) == 2


def case_contact_filter_matches_contact_or_address(store):
    store.add(
        [
            _msg(1, contact="Pat Neighbour", address="+15550100"),
            _msg(2, contact=None, address="+15550100"),
            _msg(3, contact="Sam", address="+15550333"),
        ]
    )
    assert sorted(m.id for m in store.search(None, None, "pat", 10)) == ["id1"]
    assert sorted(m.id for m in store.search(None, None, "0100", 10)) == ["id1", "id2"]
    assert sorted(m.id for m in store.search(None, None, " SAM ", 10)) == ["id3"]
    assert store.search(None, None, "nobody", 10) == []


def case_contact_and_query_combine(store):
    store.add(
        [
            _msg(1, contact="Pat", body="about the car"),
            _msg(2, contact="Pat", body="lunch"),
            _msg(3, contact="Sam", body="about the car"),
        ]
    )
    assert [m.id for m in store.search("car", None, "pat", 10)] == ["id1"]


def case_since_aware_and_naive(store):
    store.add([_msg(1, sent_at=T0 - timedelta(days=5)), _msg(2)])
    assert [m.id for m in store.recent(T0, 10)] == ["id2"]  # >= is inclusive
    assert [m.id for m in store.recent(T0.replace(tzinfo=None), 10)] == ["id2"]
    plus2 = T0.astimezone(timezone(timedelta(hours=2)))
    assert [m.id for m in store.recent(plus2, 10)] == ["id2"]
    assert len(store.search(None, T0 - timedelta(days=9), None, 10)) == 2


def case_ordering_ties_and_limit_clamp(store):
    store.add([_msg(i) for i in range(1, 4)] + [_msg(9, sent_at=T0 + timedelta(hours=1))])
    out = store.recent(None, 10)
    assert out[0].id == "id9"
    assert [m.id for m in out[1:]] == ["id1", "id2", "id3"]
    assert len(store.recent(None, 2)) == 2
    assert len(store.recent(None, 0)) == 1
    assert len(store.search(None, None, None, -5)) == 1
    assert len(store.search("body", None, None, 0)) == 1


def case_round_trip_fields(store):
    sent = T0.astimezone(timezone(timedelta(hours=-5)))
    original = _msg(1, contact="Ann", direction="out", sent_at=sent)
    store.add([original])
    (got,) = store.recent(None, 10)
    assert got == original


def case_substring_search_matches_inside_words(store):
    store.add([_msg(1, body="reimbursement pending"), _msg(2, body="Q3")])
    assert [m.id for m in store.search("burse", None, None, 10)] == ["id1"]
    assert [m.id for m in store.search("q3", None, None, 10)] == ["id2"]


def case_like_wildcards_are_literal(store):
    store.add([_msg(1, body="100% sure"), _msg(2, body="plain")])
    assert [m.id for m in store.search("100%", None, None, 10)] == ["id1"]
    assert store.search("_", None, None, 10) == []
    assert store.search(None, None, "%", 10) == []


def case_since_in_the_future_is_empty(store):
    store.add([_msg(1), _msg(2)])
    future = T0 + timedelta(days=365)
    assert store.recent(future, 10) == []
    assert store.search(None, future, None, 10) == []
    assert store.search("body", future, None, 10) == []


def case_one_character_query(store):
    store.add([_msg(1, body="a b c"), _msg(2, body="zzz")])
    assert [m.id for m in store.search("b", None, None, 10)] == ["id1"]
    assert [m.id for m in store.search("Z", None, None, 10)] == ["id2"]


CASES = [
    case_search_each_field_case_insensitive,
    case_blank_query_behaves_like_recent,
    case_contact_filter_matches_contact_or_address,
    case_contact_and_query_combine,
    case_since_aware_and_naive,
    case_ordering_ties_and_limit_clamp,
    case_round_trip_fields,
    case_substring_search_matches_inside_words,
    case_like_wildcards_are_literal,
    case_since_in_the_future_is_empty,
    case_one_character_query,
]
