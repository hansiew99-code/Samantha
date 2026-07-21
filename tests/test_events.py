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

    # Pin quiet hours far away from "now" so sweeps run deterministically.
    now = datetime.now(ZoneInfo(TZ))
    far = (now + timedelta(hours=6)).time().replace(second=0, microsecond=0)
    far_end = (now + timedelta(hours=7)).time().replace(second=0, microsecond=0)
    settings.quiet_hours = (far, far_end)
    return Sweeper(settings, bus, memory, brain, notify), client


def decisions_json(*decisions: dict) -> str:
    return json.dumps({"decisions": list(decisions)})


async def test_n_events_one_llm_call(settings, conn, memory, bus):
    for i in range(6):
        bus.enqueue("gmail", "new_email", f"sender{i}@x.com", {"snippet": f"email {i}"})

    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(decisions_json(
        {"i": 0, "action": "notify", "message": "Heads up: email 0"},
        *[{"i": i, "action": "digest"} for i in range(1, 6)],
    ))])]
    sweeper, client = make_sweeper(settings, conn, memory, bus, script, notifications)

    handled = await sweeper.run_sweep()

    assert handled == 6
    assert len(client.calls) == 1  # six events, ONE call — never one per event
    assert notifications == ["Heads up: email 0"]
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


async def test_quiet_hours_defer_sweeps(settings, conn, memory, bus):
    bus.enqueue("gmail", "new_email", "a@b.c", {"snippet": "late email"})
    notifications: list[str] = []
    sweeper, client = make_sweeper(settings, conn, memory, bus, script=[], notifications=notifications)
    now = datetime.now(ZoneInfo(TZ))
    settings.quiet_hours = (
        (now - timedelta(hours=1)).time(),
        (now + timedelta(hours=1)).time(),
    )
    assert await sweeper.run_sweep() == 0
    assert client.calls == []
    # Event still pending — the morning digest will pick it up.
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
    assert notifications == ["❗ Boss wants a call"]


def test_in_quiet_hours_wraps_midnight():
    quiet = (time(23, 0), time(7, 0))
    tz = ZoneInfo(TZ)
    assert in_quiet_hours(datetime(2026, 7, 21, 23, 30, tzinfo=tz), quiet)
    assert in_quiet_hours(datetime(2026, 7, 21, 3, 0, tzinfo=tz), quiet)
    assert not in_quiet_hours(datetime(2026, 7, 21, 12, 0, tzinfo=tz), quiet)
