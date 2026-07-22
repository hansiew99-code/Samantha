"""Morning brief (Sonnet) and evening review (Haiku) — BRIEF §6.4.

Gathering is deterministic code; the LLM's only job is phrasing one digest,
one call. Nothing is pushed during quiet hours except explicit reminders.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .brain import Brain
from .config import Settings
from .context import assemble_base
from .events import EventBus
from .governor import DEGRADED, DETERMINISTIC
from .memory import Memory
from .router import HAIKU, SONNET

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]

MORNING_INSTRUCTIONS = """\
Write the owner's morning brief in your own texting voice — the way you'd tap \
it out, not a formatted report. Open with one line on the shape of the day, \
then the calendar (times + anything they'd want flagged: a moved meeting, a \
conflict, a tight turnaround), then what actually needs them today — tasks \
due, unread email or Google Chat messages that genuinely matter (someone \
waiting on a reply, a deadline), decisions sitting open. Call out the one or \
two things that will bite if ignored. Skip empty sections without announcing \
them. Under 150 words, \
and sound like someone who's already looked at everything."""

EVENING_INSTRUCTIONS = """\
Write a short evening review in your own voice from the data below: what's on \
tomorrow morning, and any loose end from today still hanging (an unanswered \
email or Chat message that matters, a task that slipped). Max 60 words. If \
there's genuinely \
nothing useful to say, reply with exactly NOTHING."""


class DigestService:
    def __init__(
        self,
        settings: Settings,
        memory: Memory,
        bus: EventBus,
        brain: Brain | None,
        notify: Notify,
        gcal=None,
        gmail=None,
        gchat=None,
        conn=None,
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.bus = bus
        self.brain = brain
        self.notify = notify
        self.gcal = gcal
        self.gmail = gmail
        self.gchat = gchat
        self.conn = conn if conn is not None else memory.conn

    async def morning(self) -> None:
        await self._digest(SONNET, MORNING_INSTRUCTIONS, "digest")

    async def evening(self) -> None:
        await self._digest(HAIKU, EVENING_INSTRUCTIONS, "digest")

    async def _digest(self, model: str, instructions: str, purpose: str) -> None:
        if self.brain is None:
            return
        data = await self._gather()
        mode = self.brain.governor.mode()
        if mode == DETERMINISTIC:
            # Budget exhausted: a plain-text digest costs zero tokens.
            text = self._plain_digest(data)
            if text:
                await self.notify(text)
            self._consume_backlog()
            return
        if mode == DEGRADED:
            model = HAIKU
            instructions += "\n(Budget is tight today — keep it extremely short.)"
        system = assemble_base(self.memory)
        system.append({"type": "text", "text": instructions})
        text = await self.brain.run_loop(
            model,
            system,
            [{"role": "user", "content": json.dumps(data, ensure_ascii=False, default=str)}],
            purpose=purpose,
            tools=[],
        )
        if text.strip() and text.strip() != "NOTHING":
            await self.notify(text.strip())
            self.memory.log_message("assistant", text.strip())
        self._consume_backlog()

    def _consume_backlog(self) -> None:
        for ev in self.bus.digest_backlog():
            if ev["processed_at"] is None:
                self.bus.mark(ev["id"], "digest")

    @staticmethod
    def _plain_digest(data: dict) -> str:
        lines = ["(zero-token digest — daily budget exhausted)"]
        for ev in data.get("calendar_next_48h", [])[:6]:
            lines.append(f"📅 {ev['start']}: {ev['summary']}")
        for t in data.get("open_tasks", [])[:5]:
            due = f" (due {t['due_at']})" if t.get("due_at") else ""
            lines.append(f"☑️ {t['title']}{due}")
        for item in data.get("backlog", [])[:5]:
            lines.append(f"• [{item['source']}] {item['summary']}")
        return "\n".join(lines) if len(lines) > 1 else ""

    async def _gather(self) -> dict:
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(tz)
        data: dict = {"now": now.isoformat()}

        if self.gcal is not None:
            try:
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                end = start + timedelta(days=2)
                data["calendar_next_48h"] = await asyncio.to_thread(
                    self.gcal.list_events, start.isoformat(), end.isoformat()
                )
            except Exception:
                log.exception("digest: calendar fetch failed")

        # A morning brief that ignores the inbox isn't a brief. Pull recent
        # unread from the primary category so the digest can flag anything the
        # owner still owes a reply on — deterministic gather, LLM just phrases.
        if self.gmail is not None:
            try:
                data["unread_email"] = await asyncio.to_thread(
                    self.gmail.search,
                    "in:inbox is:unread newer_than:2d category:primary",
                    8,
                )
            except Exception:
                log.exception("digest: gmail fetch failed")

        # Read Google Chat on her own, the same as the inbox — recent messages
        # from other people the owner may not have replied to yet.
        if self.gchat is not None:
            try:
                since = (now - timedelta(days=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                data["chat_messages"] = await asyncio.to_thread(self.gchat.recent_inbound, since, 5)
            except Exception:
                log.exception("digest: gchat fetch failed")

        data["open_tasks"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT source, title, due_at FROM tasks WHERE status = 'open' "
                "ORDER BY due_at IS NULL, due_at LIMIT 15"
            )
        ]
        data["upcoming_reminders"] = [
            {"due_at": r["due_at"], "text": r["text"]}
            for r in self.conn.execute(
                "SELECT due_at, text FROM reminders WHERE status IN ('scheduled','snoozed') "
                "ORDER BY due_at LIMIT 10"
            )
        ]
        backlog = []
        for ev in self.bus.digest_backlog():
            payload = json.loads(ev["payload"])
            backlog.append(
                {
                    "source": ev["source"],
                    "kind": ev["kind"],
                    "from": ev["scope"],
                    "summary": str(
                        payload.get("subject") or payload.get("title") or payload.get("text") or payload.get("snippet", "")
                    )[:150],
                }
            )
        data["backlog"] = backlog[:25]
        return data
