"""``pdl-assistant-flagger``: run the owner's threshold checks and push each new flag once."""

import asyncio
import logging
import os
import sqlite3
import sys
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

from .__main__ import base_parser, build_clients, read_client_secret
from .config import AssistantConfig, ConfigError, load_config
from .flagger import FlaggerOutcome, run_flagger
from .flagger_config import FlaggerConfigError, load_checks
from .notify import Notifier, notifier_from_config

logger = logging.getLogger("pdl_assistant.flagger")


def _exit_code(outcome: FlaggerOutcome, check_count: int) -> int:
    if outcome.status == "notify_failed":
        return 1
    if check_count and len(outcome.failed_checks) >= check_count:
        return 1
    return 0


async def _run(args: Namespace, config: AssistantConfig, notifier: Notifier) -> int:
    if not config.flagger_config_file:
        raise ConfigError("flagger_config_file is not set")
    checks = load_checks(Path(config.flagger_config_file))
    memory, gateway, llm = build_clients(config, read_client_secret(config.client_secret_file))
    try:
        outcome = await run_flagger(
            checks,
            gateway,
            memory,
            notifier,
            now=lambda: datetime.now(UTC),
            lookback_days=config.flagger_lookback_days,
            probe=llm.probe if config.flagger_require_brain else None,
            dry_run=args.dry_run,
        )
    finally:
        for closer in (gateway.aclose, llm.aclose):
            try:
                await closer()
            except Exception:
                logger.exception("error while closing a client")
        memory.close()
    failed = ",".join(outcome.failed_checks) or "-"
    logger.info(
        "flagger %s new=%d notified=%d failed_checks=%s",
        outcome.status,
        outcome.new_flags,
        outcome.notified,
        failed,
    )
    return _exit_code(outcome, len(checks))


def main(argv: list[str] | None = None) -> int:
    """0 for quiet, notified or skipped; 1 for a failed send, all checks failing or bad config."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = base_parser("pdl-assistant-flagger", __doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="log what would be sent; write nothing"
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, os.environ)
        notifier = notifier_from_config(config)
        return asyncio.run(_run(args, config, notifier))
    except (ConfigError, FlaggerConfigError, OSError, sqlite3.Error) as exc:
        sys.stderr.write(f"pdl-assistant-flagger: {exc}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
