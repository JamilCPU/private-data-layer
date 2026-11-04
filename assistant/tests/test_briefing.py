"""Tests for briefing assembly and the briefings table."""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from pdl_assistant.briefing import BRIEFING_MAX_CHARS, BriefingBuilder
from pdl_assistant.briefing_sections import SECTIONS, SectionResult
from pdl_assistant.memory import Briefing, MemoryStore

NOW = datetime(2026, 11, 4, 7, 30, tzinfo=UTC)
TODAY = date(2026, 11, 4)
HEADER = "Morning briefing, Wednesday 4 November"


@dataclass
class FakeSections:
    texts: dict[str, str] = field(default_factory=dict)
    seen: list[tuple[str, datetime]] = field(default_factory=list)

    async def run(self, name: str, since: datetime) -> SectionResult:
        self.seen.append((name, since))
        return SectionResult(name, self.texts.get(name, f"{name.title()}: ok"), True)


@pytest.fixture
def store(tmp_path: Path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


def _builder(sections: FakeSections, store: MemoryStore, **kwargs) -> BriefingBuilder:
    return BriefingBuilder(
        sections,  # type: ignore[arg-type]
        store,
        today=lambda: TODAY,
        now=lambda: NOW,
        **kwargs,
    )


async def test_header_order_and_blocks(store: MemoryStore):
    sections = FakeSections()
    text = await _builder(sections, store).build()
    expected = [HEADER, *(f"{name.title()}: ok" for name in SECTIONS)]
    assert text == "\n\n".join(expected)
    assert [name for name, _ in sections.seen] == list(SECTIONS)


async def test_since_fallback_is_24h(store: MemoryStore):
    sections = FakeSections()
    await _builder(sections, store).build()
    assert {since for _, since in sections.seen} == {NOW - timedelta(hours=24)}


async def test_since_is_last_sent(store: MemoryStore):
    sent = datetime(2026, 11, 3, 7, 0, tzinfo=UTC)
    store.record_briefing(sent.isoformat(), "sent", "old", None)
    store.record_briefing(NOW.isoformat(), "failed", "", "x")
    sections = FakeSections()
    await _builder(sections, store).build()
    assert {since for _, since in sections.seen} == {sent}


async def test_skipped_prefix(store: MemoryStore):
    store.record_briefing(NOW.isoformat(), "skipped", "", "offline")
    text = await _builder(FakeSections(), store).build()
    prefix = "Yesterday's briefing was skipped (brain offline)."
    assert text.startswith(f"{prefix}\n\n{HEADER}")


async def test_no_prefix_after_sent(store: MemoryStore):
    store.record_briefing(NOW.isoformat(), "sent", "t", None)
    text = await _builder(FakeSections(), store).build()
    assert text.startswith(HEADER)


async def test_cap(store: MemoryStore):
    sections = FakeSections({"email": "Email: " + "x" * 500})
    text = await _builder(sections, store, max_chars=100).build()
    assert len(text) == 100
    assert text.endswith("…")


def test_default_cap():
    assert BRIEFING_MAX_CHARS == 3500


def test_briefing_round_trip(store: MemoryStore):
    assert store.last_briefing() is None
    assert store.last_sent_briefing_at() is None
    first = store.record_briefing("2026-11-03T07:00:00+00:00", "sent", "hello", None)
    second = store.record_briefing("2026-11-04T07:00:00+00:00", "skipped", "", "offline")
    last = store.last_briefing()
    assert isinstance(last, Briefing)
    assert (last.id, last.status, last.text, last.note) == (second, "skipped", "", "offline")
    assert last.finished_at
    assert first < second
    assert store.last_sent_briefing_at() == datetime(2026, 11, 3, 7, 0, tzinfo=UTC)


def test_briefing_status_checked(store: MemoryStore):
    with pytest.raises(sqlite3.IntegrityError):
        store.record_briefing("2026-11-03T07:00:00+00:00", "bogus", "", None)
