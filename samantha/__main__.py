"""Daemon entrypoint: python -m samantha [--check-config] [--dry-run]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .app import build_app
from .config import Settings, load_settings

log = logging.getLogger("samantha")


async def run(settings: Settings) -> None:
    app = build_app(settings)
    await app.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("samantha %s up (db=%s)", __version__, settings.db_path)
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        await app.stop()


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

    problems = settings.validate()

    if args.check_config:
        print(settings.summary())
        if problems:
            print("\nProblems:")
            for p in problems:
                print(f"  ✗ {p}")
        else:
            print("\nAll required credentials look valid.")
        sys.exit(1 if problems else 0)

    if problems and not settings.dry_run:
        print("Cannot start — fix these first (see .env):", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
