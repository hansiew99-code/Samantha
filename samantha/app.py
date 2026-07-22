"""Application assembly: builds and wires every component. Each phase of the
brief extends this in one place instead of rewriting the entrypoint.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .actions import PendingActions
from .brain import Brain
from .callbacks import CallbackRouter
from .config import Settings
from .consolidation import Consolidator
from .db import connect
from .digests import DigestService
from .events import EventBus, Sweeper
from .governor import Governor
from .integrations import google_auth
from .integrations.clickup import ClickUpClient, ClickUpSync
from .integrations.gcal import GCalClient
from .integrations.gmail import GmailClient
from .integrations.slack import SlackService
from .memory import Memory
from .reminders import ReminderService
from .rules import RulesEngine
from .telegram_gateway import TelegramGateway
from .tools import ToolRegistry
from .tools import (
    calendar_tools,
    clickup_tools,
    gmail_tools,
    memory_tools,
    reminder_tools,
    rules_tools,
    slack_tools,
)

log = logging.getLogger(__name__)

GMAIL_POLL_MINUTES = 5
CLICKUP_POLL_MINUTES = 10
SWEEP_MINUTES = 30


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
    rules: RulesEngine = None
    bus: EventBus = None
    sweeper: Sweeper | None = None
    digests: DigestService | None = None
    consolidator: Consolidator | None = None
    brain: Brain | None = None
    gateway: TelegramGateway | None = None
    gcal: GCalClient | None = None
    gmail: GmailClient | None = None
    slack: SlackService | None = None
    clickup_sync: ClickUpSync | None = None
    callbacks: CallbackRouter = field(default_factory=CallbackRouter)

    async def start(self) -> None:
        self.scheduler.start()
        self.reminders.rehydrate()
        if self.slack and not self.settings.dry_run:
            await asyncio.to_thread(self.slack.start)
        if self.gateway:
            await self.gateway.start()

    async def stop(self) -> None:
        if self.gateway:
            await self.gateway.stop()
        if self.slack:
            await asyncio.to_thread(self.slack.stop)
        self.scheduler.shutdown(wait=False)
        self.conn.close()

    async def notify(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        if self.gateway:
            await self.gateway.send(text, reply_markup=reply_markup)
        else:
            log.info("[no gateway] notify: %s", text)


def build_app(settings: Settings) -> App:
    app = App(settings=settings)
    app.conn = connect(settings.db_path)
    app.scheduler = AsyncIOScheduler(timezone=settings.timezone)
    app.memory = Memory(app.conn)
    app.governor = Governor(app.conn, settings.daily_budget_usd)
    app.registry = ToolRegistry()
    app.actions = PendingActions(app.conn)
    app.rules = RulesEngine(app.conn, app.memory)
    app.bus = EventBus(app.conn, app.rules)

    _wire_reminders(app)
    memory_tools.register(app.registry, app.memory)
    reminder_tools.register(app.registry, app.reminders)
    rules_tools.register(app.registry, app.rules)
    _wire_google(app)
    _wire_slack(app)
    _wire_clickup(app)
    _wire_approvals(app)

    if settings.anthropic_enabled:
        app.brain = Brain(settings, app.memory, app.registry, app.governor)

    _wire_proactivity(app)
    _wire_consolidation(app)

    if settings.telegram_enabled and not settings.dry_run:
        app.gateway = TelegramGateway(
            settings,
            on_message=app.brain.handle_message if app.brain else None,
            on_callback=app.callbacks.dispatch,
        )
        _register_commands(app)
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
    gmail_tools.register(app.registry, app.gmail, app.actions, _draft_notifier(app))

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
            app.bus.enqueue("gmail", "new_email", msg.get("from", "*"), msg)
        if new:
            log.info("gmail: %d new message(s) enqueued", len(new))

    if not settings.dry_run:
        app.scheduler.add_job(poll_gmail, "interval", minutes=GMAIL_POLL_MINUTES, id="gmail-poll")
    log.info("google: calendar + gmail enabled")


# -- slack --------------------------------------------------------------------


def _wire_slack(app: App) -> None:
    settings = app.settings
    if not settings.slack_enabled:
        log.info("slack: disabled")
        return
    app.slack = SlackService(settings.slack_bot_token, settings.slack_app_token, app.bus)
    slack_tools.register(app.registry, app.conn, app.actions, _draft_notifier(app))

    async def slack_send_executor(payload: dict) -> str:
        if settings.dry_run:
            log.info("[dry-run] slack send: %s", payload)
            return "Dry run — message not actually posted."
        await asyncio.to_thread(app.slack.post_message, payload["channel"], payload["text"])
        return f"Posted to {payload['channel']} ✓"

    app.actions.register_executor("slack_send", slack_send_executor)
    log.info("slack: enabled (socket mode)")


# -- clickup ------------------------------------------------------------------


def _wire_clickup(app: App) -> None:
    settings = app.settings
    if not settings.clickup_enabled:
        log.info("clickup: disabled")
        return
    client = ClickUpClient(settings.clickup_api_token, settings.clickup_team_id)
    app.clickup_sync = ClickUpSync(client, app.conn, app.bus)
    clickup_tools.register(app.registry, app.conn, client, app.clickup_sync)
    if not settings.dry_run:
        app.scheduler.add_job(
            app.clickup_sync.poll, "interval", minutes=CLICKUP_POLL_MINUTES, id="clickup-poll"
        )
    log.info("clickup: enabled")


# -- proactivity: sweeps + digests -------------------------------------------


def _wire_proactivity(app: App) -> None:
    settings = app.settings
    app.sweeper = Sweeper(settings, app.bus, app.memory, app.brain, app.notify)
    app.digests = DigestService(
        settings, app.memory, app.bus, app.brain, app.notify,
        gcal=app.gcal, gmail=app.gmail, conn=app.conn,
    )
    if settings.dry_run:
        return  # sweeps and digests call the LLM — no background spend in a dry run
    app.scheduler.add_job(
        app.sweeper.run_sweep, "interval", minutes=SWEEP_MINUTES, id="sweep"
    )
    app.scheduler.add_job(
        app.digests.morning,
        CronTrigger(hour=settings.morning_digest.hour, minute=settings.morning_digest.minute),
        id="digest-morning",
    )
    app.scheduler.add_job(
        app.digests.evening,
        CronTrigger(hour=settings.evening_digest.hour, minute=settings.evening_digest.minute),
        id="digest-evening",
    )


# -- nightly consolidation + commands ----------------------------------------


def _wire_consolidation(app: App) -> None:
    if not app.settings.anthropic_enabled or app.settings.dry_run:
        return
    app.consolidator = Consolidator(
        app.settings, app.conn, app.memory, app.governor
    )
    app.scheduler.add_job(
        app.consolidator.run, CronTrigger(hour=3, minute=0), id="consolidation",
        misfire_grace_time=3600,
    )


def _register_commands(app: App) -> None:
    async def cmd_spend(_text: str) -> str:
        return app.governor.spend_report()

    app.gateway.register_command("spend", cmd_spend)


# -- outbound approval gate ---------------------------------------------------


def _draft_notifier(app: App):
    async def notify_draft(action_id: int, preview: str) -> None:
        await app.notify(preview, reply_markup=_approval_keyboard(action_id))

    return notify_draft


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
