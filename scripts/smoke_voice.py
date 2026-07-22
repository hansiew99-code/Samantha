#!/usr/bin/env python3
"""Live, non-delivering acceptance test for Samantha's voice and continuity.

Uses a temporary database and a synthetic Calendar tool. It never starts
Telegram, reads Google, or changes production state. It does make a few small
Anthropic calls so the deployed model—not only prompt-string tests—must pass the
two owner-supplied Telegram regressions before the service is restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from samantha.brain import Brain
from samantha.config import load_settings
from samantha.context import assemble_base
from samantha.db import connect
from samantha.digests import (
    DIGEST_OUTPUT_INSTRUCTIONS,
    EVENING_INSTRUCTIONS,
    DigestService,
)
from samantha.governor import Governor
from samantha.memory import Memory
from samantha.reminders import ReminderService
from samantha.router import HAIKU
from samantha.tools import Tool, ToolRegistry
from samantha.tools import reminder_tools


def _digest_fixture(now: datetime) -> dict:
    tomorrow = now + timedelta(days=1)
    return {
        "now": now.isoformat(),
        "calendar_next_48h": [
            {
                "item_key": "calendar:creative",
                "summary": "Creative check-in",
                "when": "tomorrow at 09:30",
                "local_due": tomorrow.replace(
                    hour=9, minute=30, second=0, microsecond=0
                ).isoformat(),
                "time_bucket": "tomorrow",
            },
            {
                "item_key": "calendar:phillip",
                "summary": "Ad feedback with Phillip",
                "when": "tomorrow at 14:00",
                "local_due": tomorrow.replace(
                    hour=14, minute=0, second=0, microsecond=0
                ).isoformat(),
                "time_bucket": "tomorrow",
            },
        ],
        "backlog": [
            {
                "event_id": 1,
                "item_key": "gchat:edm",
                "source": "gchat",
                "from": "Creative team",
                "summary": "EDM slides 100 and 101 need feedback",
            },
            {
                "event_id": 2,
                "item_key": "gmail:facebook",
                "source": "gmail",
                "from": "Ocean team",
                "subject": "Facebook access",
                "summary": "Facebook access still blocks the Ocean ad setup",
            },
        ],
        "recent_pushes": [{
            "sent_at": now.isoformat(),
            "text": (
                "Google Chat — EDM slides 100 and 101 need your feedback. "
                "Facebook access is still tangled and blocks direct boosts."
            ),
        }],
        "unread_email": [],
        "chat_messages": [],
        "open_tasks": [],
        "open_loops": [],
        "source_status": {},
    }


async def _run(env_file: Path) -> None:
    os.chdir(env_file.parent)
    settings = load_settings(str(env_file))
    settings.dry_run = True
    if not settings.anthropic_enabled:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")

    zone = ZoneInfo(settings.timezone)
    now = datetime.now(zone)
    meeting = (now + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )

    with tempfile.TemporaryDirectory(prefix="samantha-voice-smoke-") as tmp:
        settings.db_path = Path(tmp) / "smoke.db"
        conn = connect(settings.db_path)
        scheduler = AsyncIOScheduler(
            timezone=settings.timezone,
            event_loop=asyncio.get_running_loop(),
        )
        scheduler.start()
        registry = ToolRegistry()
        calendar_calls: list[tuple[str, str]] = []

        def calendar_list_events(start: str, end: str) -> str:
            calendar_calls.append((start, end))
            return (
                f"[smoke-phillip] {meeting.isoformat()} → "
                f"{(meeting + timedelta(hours=1)).isoformat()}: "
                "Ad feedback with Phillip"
            )

        registry.register(Tool(
            name="calendar_list_events",
            description=(
                "List current Calendar events. Use this before resolving an "
                "implicit meeting time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
                "required": ["start", "end"],
            },
            func=calendar_list_events,
        ))
        reminder_tools.register(
            registry,
            ReminderService(
                conn,
                scheduler,
                settings.timezone,
                deliver=None,
            ),
        )
        memory = Memory(conn)
        brain = Brain(
            settings,
            memory,
            registry,
            Governor(conn, settings.daily_budget_usd, settings.timezone),
        )

        try:
            data = _digest_fixture(now)
            system = assemble_base(memory)
            system.append({
                "type": "text",
                "text": EVENING_INSTRUCTIONS + "\n\n" + DIGEST_OUTPUT_INSTRUCTIONS,
            })
            raw = await brain.run_loop(
                HAIKU,
                system,
                [{"role": "user", "content": json.dumps(data)}],
                purpose="voice_smoke",
                tools=[],
            )
            digest, _ids, _keys = DigestService._parse_output(raw, data)
            digest, _clipped = DigestService._enforce_delivery_shape(
                digest, max_words=35, max_sentences=2
            )
            lowered = digest.casefold().replace("’", "'")
            banned = (
                "things need your attention",
                "things waiting on you",
                "quick update",
                "here's your update",
                "status update",
                "heads up",
                "flagged",
                "blocker",
                "move forward",
                "•",
            )
            if not digest or len(digest.split()) > 35:
                raise RuntimeError(f"voice smoke: invalid digest length: {digest!r}")
            if any(phrase in lowered for phrase in banned):
                raise RuntimeError(f"voice smoke: robotic digest copy: {digest!r}")
            if re.search(r"(?m)^\s*(?:[-*•]|\d{1,2}[.)])\s+", digest):
                raise RuntimeError(f"voice smoke: list-shaped digest: {digest!r}")
            repeated = {
                token
                for token in ("edm", "facebook", "ocean", "direct boosts")
                if token in lowered
            }
            if repeated:
                raise RuntimeError(
                    "voice smoke: repeated recent push items "
                    f"{sorted(repeated)}: {digest!r}"
                )
            print(f"digest voice accepted: {digest}", flush=True)

            memory.log_message(
                "assistant",
                (
                    "Tomorrow's packed. Creative check-in at 9:30am, then ad "
                    "feedback with Phillip at 2pm (Calendar)."
                ),
                channel="telegram_push",
            )
            reply = await brain.handle_message(
                "Cool, remind me before the meeting to ask Phil about how the "
                "referencer should look and the folder structure for uploads."
            )
            reply_text = str(getattr(reply, "text", reply))
            row = conn.execute(
                "SELECT text, due_at FROM reminders ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None or not calendar_calls:
                raise RuntimeError("voice smoke: Calendar-backed reminder was not set")
            stored_due = datetime.fromisoformat(row["due_at"]).astimezone(zone)
            if stored_due != meeting - timedelta(minutes=10):
                raise RuntimeError(
                    f"voice smoke: reminder due {stored_due.isoformat()}, expected "
                    f"{(meeting - timedelta(minutes=10)).isoformat()}"
                )
            reply_lower = reply_text.casefold()
            if not all(
                token in reply_lower
                for token in ("1:50 pm", "referencer", "folder")
            ):
                raise RuntimeError(
                    f"voice smoke: vague reminder confirmation: {reply_text!r}"
                )
            if "?" in reply_text or reply_lower.strip() in {"done", "done."}:
                raise RuntimeError(
                    f"voice smoke: reminder asked/replied weakly: {reply_text!r}"
                )
            print(f"reminder continuity accepted: {reply_text}", flush=True)
        finally:
            scheduler.shutdown(wait=False)
            close = getattr(brain.client, "close", None)
            if close is not None:
                await close()
            conn.close()


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
