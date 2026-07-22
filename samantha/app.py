"""Application assembly: builds and wires every component. Each phase of the
brief extends this in one place instead of rewriting the entrypoint.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .actions import PendingActions
from .brain import Brain
from .callbacks import CallbackRouter
from .config import Settings
from .consolidation import Consolidator
from .db import connect, kv_set, record_sync_failure, record_sync_success
from .digests import DigestService
from .events import EventBus, Sweeper
from .governor import Governor
from .integrations import google_auth
from .integrations.clickup import ClickUpClient, ClickUpSync
from .integrations.gcal import GCalClient
from .integrations.gchat import GChatClient, PartialGoogleChatReadError
from .integrations.gmail import (
    GmailClient,
    INGESTION_CHECKPOINT_KEY,
    PartialGmailReadError,
)
from .integrations.slack import SlackService
from .memory import Memory
from .proactive import ProactiveScanner
from .reminders import ReminderService
from .rules import RulesEngine
from .telegram_gateway import TelegramGateway
from .watchers import WatcherService
from .tools import ToolRegistry
from .tools import (
    calendar_tools,
    clickup_tools,
    gchat_tools,
    gmail_tools,
    memory_tools,
    reminder_tools,
    rules_tools,
    slack_tools,
    watcher_tools,
)

log = logging.getLogger(__name__)

GMAIL_POLL_MINUTES = 5
GCHAT_POLL_MINUTES = 3
CLICKUP_POLL_MINUTES = 10


@dataclass
class App:
    settings: Settings
    conn: object = None
    scheduler: AsyncIOScheduler = None
    memory: Memory = None
    registry: ToolRegistry = None
    governor: Governor = None
    reminders: ReminderService = None
    watchers: WatcherService = None
    actions: PendingActions = None
    rules: RulesEngine = None
    bus: EventBus = None
    sweeper: Sweeper | None = None
    scanner: ProactiveScanner | None = None
    digests: DigestService | None = None
    consolidator: Consolidator | None = None
    brain: Brain | None = None
    gateway: TelegramGateway | None = None
    gcal: GCalClient | None = None
    gmail: GmailClient | None = None
    gchat: GChatClient | None = None
    slack: SlackService | None = None
    clickup_sync: ClickUpSync | None = None
    callbacks: CallbackRouter = field(default_factory=CallbackRouter)

    async def start(self) -> None:
        # Bring the outbound transport up before starting anything that can
        # immediately replay overdue work.  Otherwise an overdue reminder can
        # run during rehydration while Telegram is still uninitialised and be
        # permanently consumed without ever reaching the owner.
        if self.gateway:
            await self.gateway.start()
        if self.settings.dry_run:
            self.scheduler.start()
            log.info("dry run: persisted reminders/watchers are not replayed or consumed")
            return

        # Calculate next-run times while keeping cron jobs paused.  Recovery
        # can safely add reminder/watcher jobs to the live scheduler, but a
        # scheduled brief cannot race the one-off restart catch-up.
        self.scheduler.start(paused=True)
        try:
            self.actions.recover_inflight()
            self.reminders.rehydrate()
            self.watchers.rehydrate()
            # Establish source health before a catch-up brief claims to have
            # checked everything.  In particular, Slack auth/connect status
            # must be known before it is summarized to the owner.
            if self.slack:
                await asyncio.to_thread(self.slack.start)
            if self.digests:
                try:
                    await self.digests.catch_up()
                except Exception:  # noqa: BLE001 — startup must survive a missed brief
                    log.exception("digest catch-up failed; scheduled work will continue")
        finally:
            self.scheduler.resume()

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
            # Proactive pushes and draft previews are conversation turns too.
            # Persist only after successful transport acceptance so a reply
            # like "yes" has the right preceding assistant context.
            try:
                self.memory.log_message("assistant", text, channel="telegram_push")
            except Exception:
                # Telegram has already accepted the push.  A history-write
                # failure must not look like transport failure to reminders,
                # watchers, sweeps, or digests and trigger a duplicate send.
                log.exception("Telegram push delivered but history logging failed")
        elif self.settings.dry_run:
            log.info("[dry-run] notify: %s", text)
        else:
            raise RuntimeError("Telegram delivery is unavailable")


def build_app(settings: Settings) -> App:
    app = App(settings=settings)
    app.conn = connect(settings.db_path)
    app.scheduler = AsyncIOScheduler(timezone=settings.timezone)
    app.memory = Memory(app.conn)
    app.governor = Governor(
        app.conn, settings.daily_budget_usd, timezone_name=settings.timezone
    )
    app.registry = ToolRegistry()
    app.actions = PendingActions(app.conn)
    app.rules = RulesEngine(app.conn, app.memory)
    app.bus = EventBus(app.conn, app.rules)

    _wire_reminders(app)
    _wire_watchers(app)
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
            on_delivered=lambda text, channel: app.memory.log_message(
                "assistant", text, channel=channel
            ),
            on_callback_received=lambda data: app.memory.log_message(
                "user", _callback_history_text(data), channel="telegram_callback"
            ),
        )
        _register_commands(app)
        if app.brain is None:
            log.warning("no ANTHROPIC_API_KEY — telegram runs in echo mode")
    return app


# -- reminders ---------------------------------------------------------------


def _wire_reminders(app: App) -> None:
    async def deliver_reminder(rid: int, text: str) -> None:
        row = app.conn.execute(
            "SELECT recurrence, delivery_version FROM reminders WHERE id = ?", (rid,)
        ).fetchone()
        recurring = bool(row and row["recurrence"])
        version = int(row["delivery_version"]) if row else 0
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Done for now" if recurring else "Done",
                callback_data=f"rem:done:{rid}:{version}",
            ),
            InlineKeyboardButton(
                "Snooze 1h", callback_data=f"rem:snooze:{rid}:{version}:60"
            ),
            InlineKeyboardButton(
                "Tomorrow", callback_data=f"rem:snooze:{rid}:{version}:tomorrow"
            ),
        ]])
        await app.notify(f"⏰ {text}", reply_markup=markup)

    app.reminders = ReminderService(
        app.conn, app.scheduler, app.settings.timezone, deliver=deliver_reminder
    )

    async def on_reminder_callback(args: list[str]) -> str | None:
        action, rid_s, *rest = args
        rid = int(rid_s)
        if action == "done":
            if not rest:
                return "That reminder button is outdated — nothing changed."
            recurring = app.reminders.mark_done(rid, expected_version=int(rest[0]))
            if recurring is None:
                return "Already handled — nothing changed."
            return (
                "Done for now ✓ — the recurring reminder stays on."
                if recurring
                else "Done ✓"
            )
        if action == "snooze":
            if len(rest) < 2:
                return "That reminder button is outdated — nothing changed."
            version, spec = int(rest[0]), rest[1]
            minutes = _minutes_until_tomorrow_9am(app.settings) if spec == "tomorrow" else int(spec)
            if app.reminders.snooze(
                rid, minutes, expected_version=version
            ) is None:
                return "Already handled — nothing changed."
            hours = minutes / 60
            return f"Snoozed {hours:.0f}h ⏳" if hours >= 1 else f"Snoozed {minutes}m ⏳"
        return None

    app.callbacks.register("rem", on_reminder_callback)


def _wire_watchers(app: App) -> None:
    async def deliver_watcher(_watcher_id: int, text: str) -> None:
        await app.notify(text)

    app.watchers = WatcherService(
        app.conn,
        app.scheduler,
        app.settings.timezone,
        deliver=deliver_watcher,
    )


# -- google (calendar + gmail) ------------------------------------------------


def _wire_google(app: App) -> None:
    settings = app.settings
    if not settings.google_enabled:
        log.info("google: disabled (no token at %s)", settings.google_token_path)
        return
    creds = google_auth.load_credentials(settings.google_token_path)
    if creds is None:
        log.warning("google: token unusable — integration disabled")
        error = RuntimeError("configured Google OAuth token is unusable")
        record_sync_failure(app.conn, "calendar", error)
        record_sync_failure(app.conn, "gmail", error)
        if settings.gchat_enabled_flag:
            record_sync_failure(app.conn, "gchat", error)
        return

    app.gcal = GCalClient(creds=creds, tz=settings.timezone)
    app.gmail = GmailClient(creds=creds)
    calendar_tools.register(
        app.registry,
        app.gcal,
        settings.timezone,
        app.actions,
        _draft_notifier(app),
    )
    gmail_tools.register(app.registry, app.gmail, app.actions, _draft_notifier(app))

    async def verify_gmail_watch(criteria: dict) -> dict | None:
        return await asyncio.to_thread(app.gmail.matches_watch, criteria)

    app.watchers.register_verifier("gmail", verify_gmail_watch)
    watcher_tools.register(app.registry, app.watchers)

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

    async def calendar_create_executor(payload: dict) -> str:
        if settings.dry_run:
            log.info("[dry-run] calendar invite: %s", payload)
            return "Dry run — calendar invite not created."
        ev = await asyncio.to_thread(
            app.gcal.create_event,
            payload["summary"],
            payload["start"],
            payload["end"],
            payload.get("description"),
            payload.get("location"),
            payload.get("attendees"),
        )
        return f"Calendar invite created ✓ ({ev['id']})"

    async def calendar_delete_executor(payload: dict) -> str:
        if settings.dry_run:
            log.info("[dry-run] calendar delete: %s", payload)
            return "Dry run — calendar event not deleted."
        await asyncio.to_thread(app.gcal.delete_event, payload["event_id"])
        return "Calendar event deleted ✓"

    async def calendar_update_executor(payload: dict) -> str:
        if settings.dry_run:
            log.info("[dry-run] shared calendar update: %s", payload)
            return "Dry run — shared calendar event not updated."
        ev = await asyncio.to_thread(
            app.gcal.update_event,
            payload["event_id"],
            send_updates=True,
            **payload["patch"],
        )
        return f"Shared calendar event updated ✓ ({ev['id']})"

    app.actions.register_executor(
        "calendar_create_with_attendees", calendar_create_executor
    )
    app.actions.register_executor(
        "calendar_update_with_attendees", calendar_update_executor
    )
    app.actions.register_executor("calendar_delete", calendar_delete_executor)

    async def poll_gmail() -> None:
        await _poll_gmail_once(app)

    if not settings.dry_run:
        app.scheduler.add_job(poll_gmail, "interval", minutes=GMAIL_POLL_MINUTES, id="gmail-poll")
    log.info("google: calendar + gmail enabled")

    _wire_gchat(app, creds)


async def _poll_gmail_once(app: App) -> None:
    """Poll Gmail once, retaining useful partial results without claiming freshness."""
    assert app.gmail is not None
    poll_started_at = datetime.now(timezone.utc).isoformat()
    try:
        new = await asyncio.to_thread(app.gmail.poll_new, app.conn)
        if app.gmail.last_poll_complete:
            record_sync_success(app.conn, "gmail")
            # Use poll start, not completion: mail arriving while the request
            # is in flight must remain eligible for a future 404 backfill.
            kv_set(app.conn, INGESTION_CHECKPOINT_KEY, poll_started_at)
        else:
            record_sync_failure(app.conn, "gmail", PartialGmailReadError())
    except Exception as exc:
        record_sync_failure(app.conn, "gmail", exc)
        log.exception("gmail poll failed")
        return
    for msg in new:
        app.watchers.observe("gmail", msg)
        app.bus.enqueue(
            "gmail",
            "new_email",
            msg.get("from", "*"),
            msg,
            dedupe_key=f"gmail:{msg.get('id')}",
        )
    if new:
        log.info("gmail: %d new message(s) enqueued", len(new))


def _wire_gchat(app: App, creds) -> None:
    settings = app.settings
    if not settings.gchat_enabled:
        log.info("gchat: disabled (set GCHAT_ENABLED=1 after re-consenting scopes)")
        return
    if not settings.gchat_self_id.startswith("users/"):
        log.error("gchat: disabled because GCHAT_SELF_ID is missing or malformed")
        return
    app.gchat = GChatClient(creds=creds, self_id=settings.gchat_self_id)
    gchat_tools.register(app.registry, app.gchat)

    async def poll_gchat() -> None:
        try:
            new = await asyncio.to_thread(app.gchat.poll_new, app.conn)
            if app.gchat.last_read_complete:
                record_sync_success(app.conn, "gchat")
            else:
                record_sync_failure(app.conn, "gchat", PartialGoogleChatReadError())
        except Exception as exc:
            record_sync_failure(app.conn, "gchat", exc)
            log.exception("gchat poll failed")
            return
        for msg in new:
            scope = msg.get("sender_name") or msg.get("sender") or "*"
            app.bus.enqueue(
                "gchat",
                "new_message",
                scope,
                msg,
                dedupe_key=f"gchat:{msg.get('name')}",
            )
        if new:
            log.info("gchat: %d new message(s) enqueued", len(new))

    if not settings.dry_run:
        app.scheduler.add_job(poll_gchat, "interval", minutes=GCHAT_POLL_MINUTES, id="gchat-poll")
    log.info("gchat: enabled")


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
        await asyncio.to_thread(
            app.slack.post_message,
            payload["channel"],
            payload["text"],
            payload.get("thread_ts"),
        )
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
    app.scanner = ProactiveScanner(settings, app.conn, app.rules, app.notify, gcal=app.gcal)
    app.digests = DigestService(
        settings, app.memory, app.bus, app.brain, app.notify,
        gcal=app.gcal, gmail=app.gmail, gchat=app.gchat,
        clickup=app.clickup_sync, conn=app.conn,
    )
    if settings.dry_run:
        return  # sweeps and digests call the LLM — no background spend in a dry run

    # Proactive heartbeat: inbox triage + calendar look-ahead every N minutes.
    # Both self-gate to the working window (weekdays 08:00–19:00 by default), so
    # off-hours they no-op and she rests. Clock-aligned via cron.
    step = settings.proactive_interval_minutes
    app.scheduler.add_job(
        app.sweeper.run_sweep,
        CronTrigger(minute=f"*/{step}", timezone=settings.timezone),
        id="sweep",
    )
    app.scheduler.add_job(
        app.scanner.scan,
        CronTrigger(minute=f"*/{step}", timezone=settings.timezone),
        id="proactive-scan",
    )

    # Briefs: morning + afternoon on working days, evening every night.
    m, a, e = settings.morning_digest, settings.afternoon_digest, settings.evening_digest
    app.scheduler.add_job(
        app.digests.morning,
        CronTrigger(
            day_of_week=settings.work_days,
            hour=m.hour,
            minute=m.minute,
            timezone=settings.timezone,
        ),
        id="digest-morning",
    )
    app.scheduler.add_job(
        app.digests.afternoon,
        CronTrigger(
            day_of_week=settings.work_days,
            hour=a.hour,
            minute=a.minute,
            timezone=settings.timezone,
        ),
        id="digest-afternoon",
    )
    app.scheduler.add_job(
        app.digests.evening,
        CronTrigger(hour=e.hour, minute=e.minute, timezone=settings.timezone),
        id="digest-evening",
    )


# -- nightly consolidation + commands ----------------------------------------


def _wire_consolidation(app: App) -> None:
    if not app.settings.anthropic_enabled or app.settings.dry_run:
        return
    app.consolidator = Consolidator(
        app.settings,
        app.conn,
        app.memory,
        app.governor,
        api_lock=app.brain._api_lock if app.brain else None,
    )
    app.scheduler.add_job(
        app.consolidator.run,
        CronTrigger(hour=3, minute=0, timezone=app.settings.timezone),
        id="consolidation",
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
            if app.actions.request_edit(aid):
                return (
                    f"Draft #{aid} is locked so it can't be sent accidentally. "
                    "Tell me what to change and I'll make a fresh approval draft."
                )
            return "That draft was already handled, so I won't edit or resend it."
        return None

    app.callbacks.register("act", on_action_callback)


def _minutes_until_tomorrow_9am(settings: Settings) -> int:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(settings.timezone))
    target = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return max(1, int((target - now).total_seconds() // 60))


def _callback_history_text(data: str) -> str:
    """Turn opaque button payloads into small, truthful owner-action turns."""
    parts = data.split(":")
    if len(parts) >= 3 and parts[0] == "act":
        verbs = {"send": "approved sending", "edit": "requested edits to", "discard": "discarded"}
        return f"[Telegram button] Owner {verbs.get(parts[1], parts[1])} draft #{parts[2]}."
    if len(parts) >= 3 and parts[0] == "rem":
        if parts[1] == "done":
            return f"[Telegram button] Owner marked reminder #{parts[2]} done for now."
        if parts[1] == "snooze":
            duration = parts[4] if len(parts) > 4 else "unknown"
            return f"[Telegram button] Owner snoozed reminder #{parts[2]} ({duration})."
    return "[Telegram button] Owner used an assistant control."
