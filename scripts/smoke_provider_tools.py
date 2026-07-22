#!/usr/bin/env python3
"""Send one harmless request with the exact configured provider tool bundle.

This is a deployment smoke test, not a daemon launch: it uses a temporary
database, never starts Telegram or background jobs, and performs no tool call.
It does make one small Anthropic request so provider-side schema rejection is
caught before production is restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from samantha.app import build_app
from samantha.brain import ESCALATE_SPEC
from samantha.config import load_settings
from samantha.replies import OwnerReply
from samantha.tools.registry import validate_provider_tools


async def _run(env_file: Path) -> None:
    # Relative token/credential paths in .env are relative to the production
    # working directory, not whichever release checkout invokes this script.
    os.chdir(env_file.parent)
    settings = load_settings(str(env_file))
    settings.dry_run = True
    if not settings.anthropic_enabled:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    with tempfile.TemporaryDirectory(prefix="samantha-provider-smoke-") as tmp:
        settings.db_path = Path(tmp) / "smoke.db"
        app = build_app(settings)
        try:
            assert app.brain is not None
            tools = sorted(
                app.registry.specs() + [ESCALATE_SPEC],
                key=lambda tool: tool["name"],
            )
            validate_provider_tools(tools)
            strict_count = sum(tool.get("strict") is True for tool in tools)
            print(
                f"provider schema candidate: {len(tools)} tools, "
                f"{strict_count} strict",
                flush=True,
            )
            reply = await app.brain.handle_message(
                "Reply with exactly: schema ok. Do not call a tool."
            )
            if isinstance(reply, OwnerReply) and reply.status == "degraded":
                raise RuntimeError("Anthropic rejected or failed the smoke request")
            if not str(getattr(reply, "text", reply)).strip():
                raise RuntimeError("Anthropic returned an empty smoke response")
            print("provider schema accepted", flush=True)
        finally:
            app.conn.close()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Production-compatible .env file (default: .env)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.env_file.resolve()))


if __name__ == "__main__":
    main()
