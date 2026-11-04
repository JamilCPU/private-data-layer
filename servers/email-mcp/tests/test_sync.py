"""sync_once tests use a small FakeSource (open/search_since/fetch_since/close).

One test also drives the real ImapSource over the existing FakeImap.
"""

import logging
from dataclasses import replace
from datetime import date

import pytest

from pdl_email.imap_source import ImapSource
from pdl_email.memory_store import MemoryStore
from pdl_email.store import SyncResult, SyncState
from pdl_email.sync import sync_once

from .fake_imap import FakeImap
from .test_memory_store import make

TODAY = date(2026, 11, 4)


class FakeSource:
    def __init__(self, uids, *, uidvalidity=1, uidnext=None, fail_after=None):
        self.uids = list(uids)
        self.uidvalidity = uidvalidity
        self.uidnext = uidnext if uidnext is not None else max(self.uids, default=0) + 1
        self.fail_after = fail_after
        self.closed = 0
        self.since_calls: list[date] = []
        self.fetch_calls: list[int] = []

    def open(self):
        return self.uidvalidity, self.uidnext

    def search_since(self, since):
        self.since_calls.append(since)
        return self.uids[0] if self.uids else self.uidnext

    def fetch_since(self, uid_from):
        self.fetch_calls.append(uid_from)
        for n, uid in enumerate(u for u in self.uids if u >= uid_from):
            if self.fail_after is not None and n >= self.fail_after:
                raise OSError("connection dropped")
            yield replace(make(uid), uidvalidity=self.uidvalidity)

    def close(self):
        self.closed += 1


def run(source, store, **kw):
    return sync_once(source, store, today=lambda: TODAY, **kw)


def test_first_sync_uses_search_since():
    store, src = MemoryStore(), FakeSource([4, 5, 6])
    assert run(src, store, initial_days=30) == SyncResult(3, 3, False)
    assert src.since_calls == [date(2026, 10, 5)]
    assert src.fetch_calls == [4]
    assert store.sync_state("INBOX") == SyncState(1, 6)
    assert src.closed == 1


def test_incremental_uses_last_uid_and_duplicates_count_zero():
    store = MemoryStore()
    run(FakeSource([1, 2]), store)
    src = FakeSource([1, 2, 3])
    assert run(src, store) == SyncResult(1, 1, False)
    assert src.fetch_calls == [3] and src.since_calls == []
    store.set_sync_state("INBOX", SyncState(1, 0))
    assert run(FakeSource([1, 2, 3]), store) == SyncResult(3, 0, False)


def test_empty_first_sync_records_uidnext_minus_one_and_noop_keeps_state():
    store = MemoryStore()
    assert run(FakeSource([], uidnext=20), store) == SyncResult(0, 0, False)
    assert store.sync_state("INBOX") == SyncState(1, 19)
    assert run(FakeSource([], uidnext=25), store).fetched == 0
    assert store.sync_state("INBOX") == SyncState(1, 19)


def test_mid_batch_failure_resumes_without_refetch():
    store = MemoryStore()
    src = FakeSource([1, 2, 3, 4, 5], fail_after=2)
    with pytest.raises(OSError):
        run(src, store, batch_size=2)
    assert src.closed == 1
    assert store.sync_state("INBOX") == SyncState(1, 2)
    nxt = FakeSource([1, 2, 3, 4, 5])
    assert run(nxt, store, batch_size=2) == SyncResult(3, 3, False)
    assert nxt.fetch_calls == [3]
    assert store.sync_state("INBOX") == SyncState(1, 5)


def test_uidvalidity_change_resets(caplog):
    store = MemoryStore()
    store.set_sync_state("INBOX", SyncState(1, 100))
    src = FakeSource([7, 8], uidvalidity=2)
    with caplog.at_level(logging.WARNING):
        assert run(src, store) == SyncResult(2, 2, True)
    assert src.since_calls and src.fetch_calls == [7]
    assert store.sync_state("INBOX") == SyncState(2, 8)
    assert "UIDVALIDITY" in caplog.text


def test_real_imap_source_over_fake_imap():
    raw = b"From: a@x.test\r\nSubject: hi\r\nDate: Tue, 03 Nov 2026 10:00:00 -0000\r\n\r\nbody"
    items = [(f"10 (UID 10 RFC822 {{{len(raw)}}}".encode(), raw), b")"]
    fake = FakeImap(search=b"10", fetch_items=items)
    store = MemoryStore()
    result = run(ImapSource("h", "u", "p", connect=fake), store)
    assert result == SyncResult(1, 1, False)
    assert store.sync_state("INBOX") == SyncState(777, 10)
    assert ("logout",) in fake.calls
