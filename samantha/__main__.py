"""Daemon entrypoint: python -m samantha [--check-config] [--dry-run]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import __version__
from .config import Settings, load_settings
from .db import connect
from .telegram_gateway import TelegramGateway

log = logging.getLogger("samantha")


async def run(settings: Settings) -> None:
    conn = connect(settings.db_path)
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    gateway: TelegramGateway | None = None
    if settings.telegram_enabled and not settings.dry_run:
        gateway = TelegramGateway(settings)
    elif settings.dry_run:
        log.info("dry run: telegram gateway not started")
    else:
        log.warning("telegram not configured — running headless (scheduler only)")

    scheduler.start()
    if gateway:
        await gateway.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("samantha %s up (db=%s)", __version__, settings.db_path)
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        if gateway:
            await gateway.stop()
        scheduler.shutdown(wait=False)
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="samantha")
    parser.add_argument("--check-config", action="store_true", help="print config summary and exit")
    parser.add_argument("--dry-run", action="store_true", help="never send anything outbound")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = load_settings(args.env_file)
    if args.dry_run:
        settings.dry_run = True

    if args.check_config:
        print(settings.summary())
        sys.exit(0)

    if not settings.anthropic_enabled and not settings.dry_run:
        print("ANTHROPIC_API_KEY is required (or use --dry-run). See .env.example.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
