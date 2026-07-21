"""Application assembly: builds and wires every component. Later phases extend
this in one place instead of rewriting the entrypoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .brain import Brain
from .callbacks import CallbackRouter
from .config import Settings
from .db import connect
from .governor import Governor
from .memory import Memory
from .reminders import ReminderService
from .telegram_gateway import TelegramGateway
from .tools import ToolRegistry
from .tools import memory_tools, reminder_tools

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    conn: object = None
    scheduler: AsyncIOScheduler = None
    memory: Memory = None
    registry: ToolRegistry = None
    governor: Governor = None
    reminders: ReminderService = None
    brain: Brain | None = None
    gateway: TelegramGateway | None = None
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


def build_app(settings: Settings) -> App:
    app = App(settings=settings)
    app.conn = connect(settings.db_path)
    app.scheduler = AsyncIOScheduler(timezone=settings.timezone)
    app.memory = Memory(app.conn)
    app.governor = Governor(app.conn, settings.daily_budget_usd)
    app.registry = ToolRegistry()

    # Reminder delivery: pure scheduler → Telegram, zero tokens.
    async def deliver_reminder(rid: int, text: str) -> None:
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Done", callback_data=f"rem:done:{rid}"),
            InlineKeyboardButton("Snooze 1h", callback_data=f"rem:snooze:{rid}:60"),
            InlineKeyboardButton("Tomorrow", callback_data=f"rem:snooze:{rid}:tomorrow"),
        ]])
        if app.gateway:
            await app.gateway.send(f"⏰ {text}", reply_markup=markup)
        else:
            log.info("[no gateway] reminder fired: %s", text)

    app.reminders = ReminderService(
        app.conn, app.scheduler, settings.timezone, deliver=deliver_reminder
    )

    # Tool surface (fixed, sorted — cache-stable).
    memory_tools.register(app.registry, app.memory)
    reminder_tools.register(app.registry, app.reminders)

    # Brain (needs an API key; without one the gateway echoes).
    if settings.anthropic_enabled:
        app.brain = Brain(settings, app.memory, app.registry, app.governor)

    # Zero-token button taps.
    async def on_reminder_callback(args: list[str]) -> str | None:
        action, rid_s, *rest = args
        rid = int(rid_s)
        if action == "done":
            app.reminders.mark_done(rid)
            return "Done ✓"
        if action == "snooze":
            spec = rest[0] if rest else "60"
            minutes = _minutes_until_tomorrow_9am(settings) if spec == "tomorrow" else int(spec)
            app.reminders.snooze(rid, minutes)
            hours = minutes / 60
            return f"Snoozed {hours:.0f}h ⏳" if hours >= 1 else f"Snoozed {minutes}m ⏳"
        return None

    app.callbacks.register("rem", on_reminder_callback)

    if settings.telegram_enabled and not settings.dry_run:
        app.gateway = TelegramGateway(
            settings,
            on_message=app.brain.handle_message if app.brain else None,
            on_callback=app.callbacks.dispatch,
        )
        if app.brain is None:
            log.warning("no ANTHROPIC_API_KEY — telegram runs in echo mode")
    return app


def _minutes_until_tomorrow_9am(settings: Settings) -> int:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(settings.timezone))
    target = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return max(1, int((target - now).total_seconds() // 60))
