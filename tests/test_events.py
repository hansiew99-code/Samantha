"""Event bus + sweeper: batching (N events → 1 call), suppression, quiet hours."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from samantha.brain import Brain
from samantha.events import EventBus, Sweeper, in_quiet_hours
from samantha.governor import Governor
from samantha.memory import Memory
from samantha.rules import RulesEngine
from samantha.tools import ToolRegistry

from fakes import FakeAnthropicClient, FakeResponse, text_block

TZ = "Asia/Kuala_Lumpur"


@pytest.fixture
def bus(conn, memory) -> EventBus:
    return EventBus(conn, RulesEngine(conn, memory))


def make_sweeper(settings, conn, memory, bus, script, notifications):
    client = FakeAnthropicClient(script)
    brain = Brain(settings, memory, ToolRegistry(), Governor(conn, 1.0), client=client)

    async def notify(text: str) -> None:
        notifications.append(text)

    # Open the working window to all hours/days so sweeps run deterministically
    # whatever time the test suite happens to run.
    settings.work_days = "mon-sun"
    settings.work_start_hour = 0
    settings.work_end_hour = 24
    return Sweeper(settings, bus, memory, brain, notify), client


def decisions_json(*decisions: dict) -> str:
    return json.dumps({"decisions": list(decisions)})


async def test_n_events_one_llm_call(settings, conn, memory, bus):
    for i in range(6):
        bus.enqueue("gmail", "new_email", f"sender{i}@x.com", {"snippet": f"email {i}"})

    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(decisions_json(
        {"i": 0, "action": "notify", "message": "Sender 0 needs a reply today."},
        *[{"i": i, "action": "digest"} for i in range(1, 6)],
    ))])]
    sweeper, client = make_sweeper(settings, conn, memory, bus, script, notifications)

    handled = await sweeper.run_sweep()

    assert handled == 6
    assert len(client.calls) == 1  # six events, ONE call — never one per event
    assert notifications == ["Gmail — Sender 0 needs a reply today."]
    dispositions = {
        r["disposition"]
        for r in conn.execute("SELECT disposition FROM events_queue WHERE processed_at IS NOT NULL")
    }
    assert dispositions == {"notified", "digest"}


async def test_suppressed_source_never_reaches_llm(settings, conn, memory, bus):
    bus.rules.add("clickup", "suppress")
    # Suppressed at enqueue time:
    assert bus.enqueue("clickup", "due_soon", "chores", {"title": "x"}) is None
    # And even a row that predates the rule is filtered at sweep time:
    conn.execute(
        "INSERT INTO events_queue(source, kind, scope, payload) VALUES ('clickup','due_soon','chores','{}')"
    )
    conn.commit()

    notifications: list[str] = []
    sweeper, client = make_sweeper(settings, conn, memory, bus, script=[], notifications=notifications)
    await sweeper.run_sweep()  # empty script → any LLM call would raise

    assert client.calls == []
    assert notifications == []
    row = conn.execute("SELECT disposition FROM events_queue").fetchone()
    assert row["disposition"] == "suppressed"


async def test_off_hours_defer_sweeps(settings, conn, memory, bus):
    bus.enqueue("gmail", "new_email", "a@b.c", {"snippet": "late email"})
    notifications: list[str] = []
    sweeper, client = make_sweeper(settings, conn, memory, bus, script=[], notifications=notifications)
    # Restrict the working window to a day that isn't today → outside work hours.
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    settings.work_days = names[(datetime.now(ZoneInfo(TZ)).weekday() + 1) % 7]
    assert await sweeper.run_sweep() == 0
    assert client.calls == []
    # Event still pending — the next brief will pick it up.
    assert len(bus.pending()) == 1


async def test_unparseable_decisions_default_to_digest(settings, conn, memory, bus):
    bus.enqueue("gmail", "new_email", "a@b.c", {"snippet": "hi"})
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("sorry, I can't do JSON today")])]
    sweeper, _ = make_sweeper(settings, conn, memory, bus, script, notifications)

    await sweeper.run_sweep()

    assert notifications == []
    row = conn.execute("SELECT disposition FROM events_queue").fetchone()
    assert row["disposition"] == "digest"  # safe default: nothing is lost


async def test_vip_notification_gets_flagged(settings, conn, memory, bus):
    bus.rules.add("gmail", "vip", scope="boss@corp.com")
    bus.enqueue("gmail", "new_email", "The Boss <boss@corp.com>", {"snippet": "call me"})
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(decisions_json(
        {"i": 0, "action": "notify", "message": "Boss wants a call"},
    ))])]
    sweeper, _ = make_sweeper(settings, conn, memory, bus, script, notifications)
    await sweeper.run_sweep()
    assert notifications == ["❗ Gmail — Boss wants a call"]


async def test_sweep_bundles_multiple_interruptions_and_supplies_time_context(
    settings, conn, memory, bus
):
    bus.enqueue(
        "gmail",
        "new_email",
        "sarah@example.com",
        {
            "subject": "Deck due today",
            "snippet": "Can you review before 4?",
            "internal_date": "1784698200000",
        },
    )
    bus.enqueue(
        "gchat",
        "new_message",
        "Marcus",
        {"text": "Client is waiting", "create_time": "2026-07-22T10:00:00Z"},
    )
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(decisions_json(
        {"i": 0, "action": "notify", "message": "Sarah needs the deck by 4."},
        {"i": 1, "action": "notify", "message": "Marcus says the client's waiting."},
    ))])]
    sweeper, client = make_sweeper(
        settings, conn, memory, bus, script, notifications
    )

    await sweeper.run_sweep()

    assert len(notifications) == 1
    assert notifications == [
        "• Gmail — Sarah needs the deck by 4.\n"
        "• Google Chat — Marcus says the client's waiting."
    ]
    assert "worth your attention" not in notifications[0].lower()
    payload = json.loads(client.calls[0]["messages"][0]["content"])
    assert payload["now"] and payload["timezone"] == TZ
    assert payload["events"][0]["subject"] == "Deck due today"
    assert payload["events"][0]["source_time"]


def test_in_quiet_hours_wraps_midnight():
    quiet = (time(23, 0), time(7, 0))
    tz = ZoneInfo(TZ)
    assert in_quiet_hours(datetime(2026, 7, 21, 23, 30, tzinfo=tz), quiet)
    assert in_quiet_hours(datetime(2026, 7, 21, 3, 0, tzinfo=tz), quiet)
    assert not in_quiet_hours(datetime(2026, 7, 21, 12, 0, tzinfo=tz), quiet)


def test_provider_event_dedupe_is_idempotent(bus, conn):
    first = bus.enqueue(
        "slack", "mention", "C123", {"text": "hello"}, dedupe_key="slack:Ev1"
    )
    second = bus.enqueue(
        "slack", "mention", "C123", {"text": "hello again"}, dedupe_key="slack:Ev1"
    )

    assert second == first
    assert conn.execute("SELECT COUNT(*) FROM events_queue").fetchone()[0] == 1
