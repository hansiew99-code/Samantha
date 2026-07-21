"""Application assembly: builds and wires every component. Later phases extend
this in one place instead of rewriting the entrypoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .actions import PendingActions
from .brain import Brain
from .callbacks import CallbackRouter
from .config import Settings
from .db import connect
from .governor import Governor
from .integrations import google_auth
from .integrations.gcal import GCalClient
from .integrations.gmail import GmailClient
from .memory import Memory
from .reminders import ReminderService
from .telegram_gateway import TelegramGateway
from .tools import ToolRegistry
from .tools import calendar_tools, gmail_tools, memory_tools, reminder_tools

log = logging.getLogger(__name__)

GMAIL_POLL_MINUTES = 5


@dataclass
class App:
    settings: Settings
    conn: object = None
    scheduler: AsyncIOScheduler = None
    memory: Memory = None
    registry: ToolRegistry = None
    governor: Governor = None
    reminders: ReminderService = None
    actions: PendingActions = None
    brain: Brain | None = None
    gateway: TelegramGateway | None = None
    gcal: GCalClient | None = None
    gmail: GmailClient | None = None
    callbacks: CallbackRouter = field(default_factory=CallbackRouter)

    async def start(self) -> None:
        self.scheduler.start()
        self.reminders.rehydrate()
        if self.gateway:
            await self.gateway.start()

    async def stop(self) -> None:
        if self.gateway:
            await self.gateway.stop()
        self.scheduler.shutdown(wait=False)
        self.conn.close()

    # -- helpers used by wiring ----------------------------------------------

    async def notify(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        if self.gateway:
            await self.gateway.send(text, reply_markup=reply_markup)
        else:
            log.info("[no gateway] notify: %s", text)

    def enqueue_event(self, source: str, kind: str, scope: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO events_queue(source, kind, scope, payload) VALUES (?, ?, ?, ?)",
            (source, kind, scope, json.dumps(payload)),
        )
        self.conn.commit()


def build_app(settings: Settings) -> App:
    app = App(settings=settings)
    app.conn = connect(settings.db_path)
    app.scheduler = AsyncIOScheduler(timezone=settings.timezone)
    app.memory = Memory(app.conn)
    app.governor = Governor(app.conn, settings.daily_budget_usd)
    app.registry = ToolRegistry()
    app.actions = PendingActions(app.conn)

    _wire_reminders(app)
    memory_tools.register(app.registry, app.memory)
    reminder_tools.register(app.registry, app.reminders)
    _wire_google(app)
    _wire_approvals(app)

    if settings.anthropic_enabled:
        app.brain = Brain(settings, app.memory, app.registry, app.governor)

    if settings.telegram_enabled and not settings.dry_run:
        app.gateway = TelegramGateway(
            settings,
            on_message=app.brain.handle_message if app.brain else None,
            on_callback=app.callbacks.dispatch,
        )
        if app.brain is None:
            log.warning("no ANTHROPIC_API_KEY — telegram runs in echo mode")
    return app


# -- reminders ---------------------------------------------------------------


def _wire_reminders(app: App) -> None:
    async def deliver_reminder(rid: int, text: str) -> None:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data=f"rem:done:{rid}"),
            InlineKeyboardButton("Snooze 1h", callback_data=f"rem:snooze:{rid}:60"),
            InlineKeyboardButton("Tomorrow", callback_data=f"rem:snooze:{rid}:tomorrow"),
        ]])
        await app.notify(f"⏰ {text}", reply_markup=markup)

    app.reminders = ReminderService(
        app.conn, app.scheduler, app.settings.timezone, deliver=deliver_reminder
    )

    async def on_reminder_callback(args: list[str]) -> str | None:
        action, rid_s, *rest = args
        rid = int(rid_s)
        if action == "done":
            app.reminders.mark_done(rid)
            return "Done ✓"
        if action == "snooze":
            spec = rest[0] if rest else "60"
            minutes = _minutes_until_tomorrow_9am(app.settings) if spec == "tomorrow" else int(spec)
            app.reminders.snooze(rid, minutes)
            hours = minutes / 60
            return f"Snoozed {hours:.0f}h ⏳" if hours >= 1 else f"Snoozed {minutes}m ⏳"
        return None

    app.callbacks.register("rem", on_reminder_callback)


# -- google (calendar + gmail) ------------------------------------------------


def _wire_google(app: App) -> None:
    settings = app.settings
    if not settings.google_enabled:
        log.info("google: disabled (no token at %s)", settings.google_token_path)
        return
    creds = google_auth.load_credentials(settings.google_token_path)
    if creds is None:
        log.warning("google: token unusable — integration disabled")
        return

    app.gcal = GCalClient(creds=creds, tz=settings.timezone)
    app.gmail = GmailClient(creds=creds)
    calendar_tools.register(app.registry, app.gcal, settings.timezone)

    async def notify_draft(action_id: int, preview: str) -> None:
        await app.notify(preview, reply_markup=_approval_keyboard(action_id))

    gmail_tools.register(app.registry, app.gmail, app.actions, notify_draft)

    async def gmail_send_executor(payload: dict) -> str:
        if settings.dry_run:
            log.info("[dry-run] gmail send: %s", payload)
            return "Dry run — email not actually sent."
        await asyncio.to_thread(
            app.gmail.send, payload["to"], payload["subject"], payload["body"],
            payload.get("thread_id"),
        )
        return f"Sent to {payload['to']} ✓"

    app.actions.register_executor("gmail_send", gmail_send_executor)

    async def poll_gmail() -> None:
        try:
            new = await asyncio.to_thread(app.gmail.poll_new, app.conn)
        except Exception:
            log.exception("gmail poll failed")
            return
        for msg in new:
            app.enqueue_event("gmail", "new_email", msg.get("from", "*"), msg)
        if new:
            log.info("gmail: %d new message(s) enqueued", len(new))

    app.scheduler.add_job(
        poll_gmail, "interval", minutes=GMAIL_POLL_MINUTES, id="gmail-poll"
    )
    log.info("google: calendar + gmail enabled")


# -- outbound approval gate ---------------------------------------------------


def _approval_keyboard(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Send", callback_data=f"act:send:{action_id}"),
        InlineKeyboardButton("Edit", callback_data=f"act:edit:{action_id}"),
        InlineKeyboardButton("Discard", callback_data=f"act:discard:{action_id}"),
    ]])


def _wire_approvals(app: App) -> None:
    async def on_action_callback(args: list[str]) -> str | None:
        verb, aid_s = args[0], args[1]
        aid = int(aid_s)
        if verb == "send":
            return await app.actions.approve(aid)
        if verb == "discard":
            return "Discarded." if app.actions.discard(aid) else "Already handled."
        if verb == "edit":
            return "Tell me what to change and I'll redraft it."
        return None

    app.callbacks.register("act", on_action_callback)


def _minutes_until_tomorrow_9am(settings: Settings) -> int:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(settings.timezone))
    target = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return max(1, int((target - now).total_seconds() // 60))
