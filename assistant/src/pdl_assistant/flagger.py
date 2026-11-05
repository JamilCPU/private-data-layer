"""The hourly flagger: evaluate checks, record new flags, push each one exactly once."""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from .flagger_checks import Flag, evaluate_all
from .flagger_config import Check
from .gateway_client import ToolResult
from .memory import MemoryStore, StoredFlag
from .notify import Notifier, NotifyError

logger = logging.getLogger("pdl_assistant.flagger")

MAX_FLAG_LINES = 10

Status = Literal["quiet", "notified", "notify_failed", "skipped_offline"]


class _Gateway(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


@dataclass(frozen=True)
class FlaggerOutcome:
    new_flags: int
    notified: int
    failed_checks: tuple[str, ...]
    status: Status


def _title(pending: Sequence[StoredFlag]) -> str:
    if len(pending) == 1:
        return f"Flagged: {pending[0].check_name}"
    return f"Flagged: {len(pending)} items"


def _body(summaries: Sequence[str]) -> str:
    lines = list(summaries[:MAX_FLAG_LINES])
    if len(summaries) > MAX_FLAG_LINES:
        lines.append(f"and {len(summaries) - MAX_FLAG_LINES} more")
    return "\n".join(lines)


def _dry_run(flags: Sequence[Flag], memory: MemoryStore, failed: tuple[str, ...]) -> FlaggerOutcome:
    fresh = [f for f in flags if not memory.has_flag(f.check, f.key)]
    for flag in fresh:
        logger.info("dry run: would record and send: %s", flag.summary)
    return FlaggerOutcome(len(fresh), 0, failed, "quiet")


async def run_flagger(
    checks: Sequence[Check],
    gateway: _Gateway,
    memory: MemoryStore,
    notifier: Notifier,
    *,
    now: Callable[[], datetime],
    lookback_days: int,
    probe: Callable[[], Awaitable[bool]] | None = None,
    dry_run: bool = False,
) -> FlaggerOutcome:
    """One flagger pass; never sends a flag twice, retries unsent ones on the next pass."""
    if probe is not None and not await probe():
        return FlaggerOutcome(0, 0, (), "skipped_offline")
    moment = now().astimezone(UTC)
    flags, failed_list = await evaluate_all(
        checks, gateway, today=moment.date(), lookback_days=lookback_days
    )
    failed = tuple(failed_list)
    if dry_run:
        return _dry_run(flags, memory, failed)
    created = moment.isoformat()
    new = sum(memory.add_flag(f.check, f.key, f.summary, created) for f in flags)
    pending = memory.unnotified_flags()
    if not pending:
        return FlaggerOutcome(new, 0, failed, "quiet")
    try:
        await notifier.send(_title(pending), _body([f.summary for f in pending]))
    except NotifyError as exc:
        logger.warning("flag notification failed: %s", exc)
        return FlaggerOutcome(new, 0, failed, "notify_failed")
    memory.mark_flags_notified([f.id for f in pending], created)
    return FlaggerOutcome(new, len(pending), failed, "notified")
