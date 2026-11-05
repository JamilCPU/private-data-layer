from datetime import UTC, datetime

import pytest

from pdl_sms_store.models import MAX_TEXT_CHARS, ValidationError, message_id, parse_incoming

NOW = datetime(2026, 11, 4, tzinfo=UTC)


def raw(**kw):
    base = {
        "provider_id": "42",
        "date_ms": 1_700_000_000_000,
        "address": " +15550100 ",
        "contact": " Alex Example ",
        "direction": "in",
        "body": "hi",
        "thread_id": "7",
    }
    base.update(kw)
    return base


def test_message_id_deterministic_and_distinct():
    assert message_id("d", "1", 5) == message_id("d", "1", 5)
    assert message_id("d", "1", 5) != message_id("d", "1", 6)
    assert message_id("d", "1", 5) != message_id("e", "1", 5)


def test_parse_full_message():
    m = parse_incoming("pixel", raw(), NOW)
    assert m.id == message_id("pixel", "42", 1_700_000_000_000)
    assert (m.address, m.contact) == ("+15550100", "Alex Example")
    assert (m.thread_id, m.direction) == ("7", "in")
    assert m.sent_at == datetime.fromtimestamp(1_700_000_000, UTC)
    assert m.received_at == NOW


def test_optional_fields_may_be_none_or_missing():
    r = raw(contact=None, thread_id=None)
    m = parse_incoming("pixel", r, NOW)
    assert m.contact is None and m.thread_id is None
    del r["contact"]
    assert parse_incoming("pixel", r, NOW).contact is None


def test_body_truncated_with_mark():
    m = parse_incoming("pixel", raw(body="x" * (MAX_TEXT_CHARS + 5)), NOW)
    assert m.body == "x" * MAX_TEXT_CHARS + " [truncated]"
    assert parse_incoming("pixel", raw(body="x" * MAX_TEXT_CHARS), NOW).body == "x" * MAX_TEXT_CHARS


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"provider_id": 5}, "provider_id"),
        ({"provider_id": ""}, "provider_id"),
        ({"provider_id": "x" * 65}, "provider_id"),
        ({"date_ms": "1"}, "date_ms"),
        ({"date_ms": True}, "date_ms"),
        ({"date_ms": -1}, "date_ms"),
        ({"address": ""}, "address"),
        ({"address": "   "}, "address"),
        ({"address": "1" * 65}, "address"),
        ({"address": None}, "address"),
        ({"contact": 3}, "contact"),
        ({"contact": "c" * 129}, "contact"),
        ({"direction": "sideways"}, "direction"),
        ({"body": None}, "body"),
        ({"thread_id": 9}, "thread_id"),
        ({"thread_id": "t" * 65}, "thread_id"),
    ],
)
def test_bad_fields_rejected(override, field):
    with pytest.raises(ValidationError) as exc:
        parse_incoming("pixel", raw(**override), NOW)
    assert exc.value.field == field
    assert str(exc.value) == f"{field}: {exc.value.reason}"


@pytest.mark.parametrize("bad", [10**15, 10**20, 2**62, 4_102_444_800_001])
def test_date_ms_out_of_range_rejected(bad):
    with pytest.raises(ValidationError) as exc:
        parse_incoming("pixel", raw(date_ms=bad), NOW)
    assert (exc.value.field, exc.value.reason) == ("date_ms", "out of range")


def test_date_ms_upper_bound_accepted():
    assert parse_incoming("pixel", raw(date_ms=4_102_444_800_000), NOW).sent_at.year == 2100


@pytest.mark.parametrize("blank", ["", "   ", " 	 "])
def test_blank_thread_id_becomes_none(blank):
    assert parse_incoming("pixel", raw(thread_id=blank), NOW).thread_id is None


def test_thread_id_is_stripped():
    assert parse_incoming("pixel", raw(thread_id=" 7 "), NOW).thread_id == "7"


def test_missing_required_key_rejected():
    r = raw()
    del r["date_ms"]
    with pytest.raises(ValidationError, match="date_ms"):
        parse_incoming("pixel", r, NOW)


@pytest.mark.parametrize("field", ["body", "contact", "address", "thread_id", "provider_id"])
def test_nul_bytes_are_stripped(field):
    m = parse_incoming("pixel", raw(**{field: "a\x00b\x00"}), NOW)
    assert "\x00" not in getattr(m, field)
    assert getattr(m, field) == "ab"


def test_nul_only_required_field_is_rejected_as_empty():
    with pytest.raises(ValidationError) as exc:
        parse_incoming("pixel", raw(address="\x00\x00"), NOW)
    assert exc.value.field == "address"


def test_nul_only_optional_field_becomes_none():
    assert parse_incoming("pixel", raw(contact="\x00"), NOW).contact is None
