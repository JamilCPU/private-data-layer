"""Incremental IMAP -> Store synchronisation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from itertools import batched
from typing import Protocol

from pdl_email.models import Message
from pdl_email.store import Store, SyncResult, SyncState

log = logging.getLogger(__name__)


class Source(Protocol):
    def open(self) -> tuple[int, int]: ...
    def search_since(self, since: date) -> int: ...
    def fetch_since(self, uid_from: int, *, limit: int = ...) -> Iterator[Message]: ...
    def close(self) -> None: ...


def sync_once(
    source: Source,
    store: Store,
    *,
    mailbox: str = "INBOX",
    initial_days: int = 90,
    batch_size: int = 50,
    fetch_limit: int = 500,
    today: Callable[[], date] = date.today,
) -> SyncResult:
    """Fetch new messages and persist them, advancing ``last_uid`` after every batch.

    The source caps each ``fetch_since`` call at ``fetch_limit`` messages; a full call means more
    may remain, so the fetch repeats from the last stored UID until a call comes up short.
    """
    try:
        uidvalidity, uidnext = source.open()
        state = store.sync_state(mailbox)
        reset = state is not None and state.uidvalidity != uidvalidity
        if reset:
            log.warning("UIDVALIDITY changed for %s; re-syncing the initial window", mailbox)
        if state is None or reset:
            start = source.search_since(today() - timedelta(days=initial_days))
            last_uid = uidnext - 1
        else:
            start = state.last_uid + 1
            last_uid = state.last_uid
        fetched = new = 0
        limit = max(1, fetch_limit)
        while True:
            round_count = 0
            for chunk in batched(source.fetch_since(start, limit=limit), max(1, batch_size)):
                new += store.upsert_messages(chunk)
                round_count += len(chunk)
                last_uid = chunk[-1].uid
                store.set_sync_state(mailbox, SyncState(uidvalidity, last_uid))
            fetched += round_count
            if round_count < limit:
                break
            if last_uid < start:
                # Progress guard: a source returning UIDs below the requested start would make
                # this loop spin forever on the same range. Stop; the next run retries.
                log.warning("sync %s: source returned no UID >= %d; stopping drain", mailbox, start)
                break
            start = last_uid + 1  # the source capped this call; drain the rest of the backlog
        if fetched == 0:
            store.set_sync_state(mailbox, SyncState(uidvalidity, last_uid))
        log.info("sync %s: fetched=%d new=%d reset=%s", mailbox, fetched, new, reset)
        return SyncResult(fetched=fetched, new=new, uidvalidity_reset=reset)
    finally:
        source.close()
