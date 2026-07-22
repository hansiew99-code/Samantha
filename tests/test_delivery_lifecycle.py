"""Outbound delivery must be ready before persisted work can replay."""

from __future__ import annotations

import pytest

from samantha.app import App
from samantha.telegram_gateway import TelegramGateway


class Recorder:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name

    async def start(self) -> None:
        self.events.append(f"{self.name}.start")


class SchedulerRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self) -> None:
        self.events.append("scheduler.start")


class RehydrateRecorder:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name

    def rehydrate(self) -> int:
        self.events.append(f"{self.name}.rehydrate")
        return 0


class ActionsRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def recover_inflight(self) -> int:
        self.events.append("actions.recover")
        return 0


class SlackRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self) -> None:
        self.events.append("slack.start")


class DigestRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def catch_up(self) -> None:
        self.events.append("digests.catch_up")


async def test_app_starts_gateway_before_scheduler_replays_work(settings):
    events: list[str] = []
    app = App(settings=settings)
    app.gateway = Recorder(events, "gateway")
    app.scheduler = SchedulerRecorder(events)
    app.reminders = RehydrateRecorder(events, "reminders")
    app.watchers = RehydrateRecorder(events, "watchers")
    app.actions = ActionsRecorder(events)
    settings.dry_run = False

    await app.start()

    assert events == [
        "gateway.start",
        "scheduler.start",
        "actions.recover",
        "reminders.rehydrate",
        "watchers.rehydrate",
    ]


async def test_dry_run_does_not_consume_persisted_commitments(settings):
    events: list[str] = []
    app = App(settings=settings)
    app.gateway = None
    app.scheduler = SchedulerRecorder(events)
    app.reminders = RehydrateRecorder(events, "reminders")
    app.watchers = RehydrateRecorder(events, "watchers")
    app.actions = ActionsRecorder(events)
    settings.dry_run = True

    await app.start()

    assert events == ["scheduler.start"]


async def test_source_health_is_established_before_catchup_digest(settings):
    events: list[str] = []
    app = App(settings=settings)
    app.gateway = Recorder(events, "gateway")
    app.scheduler = SchedulerRecorder(events)
    app.reminders = RehydrateRecorder(events, "reminders")
    app.watchers = RehydrateRecorder(events, "watchers")
    app.actions = ActionsRecorder(events)
    app.slack = SlackRecorder(events)
    app.digests = DigestRecorder(events)
    settings.dry_run = False

    await app.start()

    assert events.index("slack.start") < events.index("digests.catch_up")


async def test_live_telegram_send_fails_loudly_before_initialization(settings):
    settings.dry_run = False
    settings.telegram_bot_token = "test-token"
    settings.telegram_chat_id = 123
    gateway = TelegramGateway(settings)

    with pytest.raises(RuntimeError, match="not initialized"):
        await gateway.send("important reminder")


async def test_dry_run_telegram_send_does_not_require_initialization(settings):
    gateway = TelegramGateway(settings)

    await gateway.send("dry-run reminder")


async def test_delivered_push_is_not_retried_when_only_history_logging_fails(settings):
    sends: list[str] = []

    class Gateway:
        async def send(self, text, reply_markup=None):
            sends.append(text)

    class BrokenMemory:
        def log_message(self, *_args, **_kwargs):
            raise RuntimeError("sqlite locked")

    app = App(settings=settings)
    app.gateway = Gateway()
    app.memory = BrokenMemory()

    await app.notify("accepted by Telegram")

    assert sends == ["accepted by Telegram"]
