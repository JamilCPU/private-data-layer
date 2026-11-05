"""``pdl-assistant-briefing``: build the morning briefing and push it to the phone."""

import asyncio
import logging
import os
import sqlite3
import sys
from argparse import Namespace
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .__main__ import base_parser, build_clients, read_client_secret
from .briefing import BriefingBuilder
from .briefing_sections import SectionRunner
from .config import AssistantConfig, ConfigError, load_config, parse_hhmm
from .memory import MemoryStore
from .notify import Notifier, NotifyError, notifier_from_config

logger = logging.getLogger("pdl_assistant.briefing")

TITLE = "Morning briefing"
OFFLINE_NOTE = "brain offline"
EXIT_OFFLINE = 2  # --now with the brain offline: nothing ran and nothing was recorded


class _Llm(Protocol):
    async def probe(self) -> bool: ...

    async def chat(self, messages: Sequence[Any], tools: Sequence[dict]) -> Any: ...


class _Gateway(Protocol):
    async def list_tools(self) -> list[Any]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


def _past_cutoff(config: AssistantConfig, moment: datetime) -> bool:
    local = moment.astimezone(ZoneInfo(config.briefing_timezone)).time()
    return local >= parse_hhmm("briefing_retry_until", config.briefing_retry_until)


async def _wait_for_brain(
    config: AssistantConfig,
    llm: _Llm,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    """Probe the model; while it is offline and before the cut-off, sleep and probe again."""
    while not await llm.probe():
        if _past_cutoff(config, now()):
            return False
        await sleep(config.briefing_retry_interval_s)
    return True


async def run_briefing(
    config: AssistantConfig,
    *,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    notifier: Notifier,
    llm: _Llm,
    gateway: _Gateway,
    memory: MemoryStore,
    retry: bool = True,
) -> str:
    """Build and send one briefing; returns ``sent``, ``skipped`` or ``failed``.

    With ``retry=False`` (``--now``) there is a single probe: an offline brain returns
    ``offline`` and records nothing.
    """
    started = now().astimezone(UTC).isoformat()
    if retry:
        online = await _wait_for_brain(config, llm, now, sleep)
    else:
        online = await llm.probe()
    if not online:
        if not retry:
            return "offline"
        memory.record_briefing(started, "skipped", "", OFFLINE_NOTE)
        return "skipped"
    zone = ZoneInfo(config.briefing_timezone)

    def today() -> date:
        return now().astimezone(zone).date()

    flag_lines = None
    # On/off switch only: the checks file must be set and present, but it is never opened here.
    if config.flagger_config_file and Path(config.flagger_config_file).exists():

        def flag_lines(since: datetime) -> list[str]:
            return [f.summary for f in memory.flags_since(since)]

    sections = SectionRunner(
        gateway,
        llm,
        today=today,
        news_count=config.briefing_news_count,
        tool_timeout_s=config.tool_timeout_s,
        flag_lines=flag_lines,
    )
    text = ""
    try:
        text = await BriefingBuilder(sections, memory, today=today, now=now).build()
        await notifier.send(TITLE, text)
    except Exception as exc:
        logger.exception("briefing failed")
        memory.record_briefing(started, "failed", text, str(exc) or type(exc).__name__)
        return "failed"
    memory.record_briefing(started, "sent", text, None)
    return "sent"


async def _send_test(notifier: Notifier) -> int:
    try:
        await notifier.send("Test", "Test from the assistant")
    except NotifyError as exc:
        logger.error("test notification failed: %s", exc)
        return 1
    logger.info("test notification sent")
    return 0


async def _run(args: Namespace, config: AssistantConfig, notifier: Notifier) -> int:
    memory, gateway, llm = build_clients(config, read_client_secret(config.client_secret_file))
    try:
        status = await run_briefing(
            config,
            now=lambda: datetime.now(UTC),
            sleep=asyncio.sleep,
            notifier=notifier,
            llm=llm,
            gateway=gateway,
            memory=memory,
            retry=not args.now,
        )
    finally:
        for closer in (gateway.aclose, llm.aclose):
            try:
                await closer()
            except Exception:
                logger.exception("error while closing a client")
        memory.close()
    logger.info("briefing %s", status)
    return {"failed": 1, "offline": EXIT_OFFLINE}.get(status, 0)


def main(argv: list[str] | None = None) -> int:
    """0 for sent or skipped, 1 for a failed briefing or bad config, 2 for ``--now`` offline."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = base_parser("pdl-assistant-briefing", __doc__)
    parser.add_argument("--now", action="store_true", help="one probe, no offline retry")
    parser.add_argument("--test-notification", action="store_true", help="send a test push, exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        notifier = notifier_from_config(config)
        if args.test_notification:
            return asyncio.run(_send_test(notifier))
        return asyncio.run(_run(args, config, notifier))
    except (ConfigError, OSError, sqlite3.Error) as exc:
        sys.stderr.write(f"pdl-assistant-briefing: {exc}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
