"""Assembles the morning briefing text from its sections."""

from collections.abc import Callable
from datetime import date, datetime, timedelta

from .briefing_sections import SECTIONS, SectionRunner
from .memory import MemoryStore

BRIEFING_MAX_CHARS = 3500
SKIPPED_PREFIX = "Yesterday's briefing was skipped (brain offline)."
ELLIPSIS = "…"


def briefing_header(day: date) -> str:
    return f"Morning briefing, {day:%A} {day.day} {day:%B}"


class BriefingBuilder:
    """Builds the briefing; the section runner makes all model calls."""

    def __init__(
        self,
        sections: SectionRunner,
        memory: MemoryStore,
        *,
        today: Callable[[], date],
        now: Callable[[], datetime],
        max_chars: int = BRIEFING_MAX_CHARS,
    ) -> None:
        self._sections = sections
        self._memory = memory
        self._today = today
        self._now = now
        self._max_chars = max_chars

    async def build(self) -> str:
        since = self._memory.last_sent_briefing_at() or self._now() - timedelta(hours=24)
        blocks = []
        last = self._memory.last_briefing()
        if last is not None and last.status == "skipped":
            blocks.append(SKIPPED_PREFIX)
        blocks.append(briefing_header(self._today()))
        for name in SECTIONS:
            blocks.append((await self._sections.run(name, since)).text)
        return self._cap("\n\n".join(blocks))

    def _cap(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        return text[: self._max_chars - len(ELLIPSIS)].rstrip() + ELLIPSIS
