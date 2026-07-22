"""Digests: one LLM call, deterministic gathering, backlog consumption."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from samantha.brain import Brain
from samantha.db import kv_get, kv_set
from samantha.digests import DigestService
from samantha.events import EventBus
from samantha.governor import Governor
from samantha.router import HAIKU, SONNET
from samantha.rules import RulesEngine
from samantha.tools import ToolRegistry

from fakes import FakeAnthropicClient, FakeResponse, text_block


@pytest.fixture
def bus(conn, memory) -> EventBus:
    return EventBus(conn, RulesEngine(conn, memory))


class FakeGmail:
    """Just enough of GmailClient for the digest gather step."""

    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        self.queries.append(query)
        return self.results


class FakeChat:
    """Just enough of GChatClient for the digest gather step."""

    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.calls: list[str] = []
        self.last_read_complete = True

    def recent_inbound(self, since_iso: str, per_space: int = 5) -> list[dict]:
        self.calls.append(since_iso)
        return self.results


def make_digests(settings, conn, memory, bus, script, notifications, gmail=None, gchat=None):
    client = FakeAnthropicClient(script)
    brain = Brain(settings, memory, ToolRegistry(), Governor(conn, 1.0), client=client)

    async def notify(text: str) -> None:
        notifications.append(text)

    return (
        DigestService(
            settings, memory, bus, brain, notify,
            gcal=None, gmail=gmail, gchat=gchat, conn=conn,
        ),
        client,
    )


async def test_morning_digest_one_sonnet_call(settings, conn, memory, bus):
    conn.execute("INSERT INTO tasks(source, title, due_at) VALUES ('clickup', 'Ship report', '2026-07-22')")
    conn.commit()
    bus.enqueue("gmail", "new_email", "a@b.c", {"subject": "Invoice"})
    for ev in bus.pending():
        bus.mark(ev["id"], "digest")

    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Morning! One task due, one email waiting.")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == SONNET
    assert notifications == ["Morning! One task due, one email waiting."]
    # The gathered data reached the model.
    payload = client.calls[0]["messages"][0]["content"]
    assert "Ship report" in payload and "Invoice" in payload


async def test_morning_brief_actively_pulls_unread_email(settings, conn, memory, bus):
    # The failure the owner complained about: a brief that never checks the
    # inbox. The digest must query Gmail and feed the unread mail to the model.
    gmail = FakeGmail(
        [{"from": "sarah@x.com", "subject": "Deck review", "snippet": "can you look today?"}]
    )
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Morning — Sarah's still waiting on the deck.")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications, gmail=gmail)

    await digests.morning()

    assert gmail.queries and "is:unread" in gmail.queries[0]  # inbox actually checked
    payload = client.calls[0]["messages"][0]["content"]
    assert "Deck review" in payload  # and the unread mail reached the model


async def test_afternoon_checkin_one_haiku_call(settings, conn, memory, bus):
    conn.execute("INSERT INTO tasks(source, title, due_at) VALUES ('local', 'Send contract', '2026-07-22')")
    conn.commit()
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Still on: 3pm sync. Contract due today.")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.afternoon()

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == HAIKU  # lighter than the morning brief
    assert notifications == ["Still on: 3pm sync. Contract due today."]


async def test_morning_brief_actively_reads_google_chat(settings, conn, memory, bus):
    # "Read the chat without me prompting" — the brief must query Chat on its
    # own and feed what it finds to the model, exactly like it does for Gmail.
    chat = FakeChat([{"sender_name": "Marcus", "text": "you around later?", "create_time": "2026-07-22T08:00:00Z"}])
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Morning — Marcus pinged you on Chat.")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications, gchat=chat)

    await digests.morning()

    assert chat.calls  # Chat was actually read, unprompted
    payload = client.calls[0]["messages"][0]["content"]
    assert "you around later?" in payload  # and the message reached the model


async def test_evening_nothing_suppresses_push(settings, conn, memory, bus):
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("NOTHING")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.evening()

    assert client.calls[0]["model"] == HAIKU
    assert notifications == []  # nothing useful → no ping (protect attention)


async def test_digest_consumes_pending_backlog(settings, conn, memory, bus):
    eid = bus.enqueue("slack", "mention", "C123", {"text": "ping"})
    assert eid is not None
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "Brief.", "included_event_ids": [eid]
    }))])]
    digests, _ = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    assert bus.pending() == []  # swept into the digest, marked processed
    assert bus.digest_backlog() == []  # and does not repeat in the next brief


async def test_digest_consumes_items_previously_marked_for_digest(
    settings, conn, memory, bus
):
    eid = bus.enqueue("gmail", "new_email", "a@b.c", {"subject": "One-time item"})
    assert eid is not None
    bus.mark(eid, "digest")
    assert len(bus.digest_backlog()) == 1

    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "One thing from your inbox.", "included_event_ids": [eid]
    }))])]
    digests, _ = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    assert bus.digest_backlog() == []
    row = conn.execute(
        "SELECT disposition FROM events_queue WHERE id = ?", (eid,)
    ).fetchone()
    assert row["disposition"] == "digested"


async def test_digest_only_consumes_backlog_rows_the_model_received(
    settings, conn, memory, bus
):
    for i in range(30):
        bus.enqueue("slack", "mention", "C123", {"text": f"item {i}"})
    included = [row["id"] for row in bus.digest_backlog()[:25]]
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "Here's the first batch.", "included_event_ids": included
    }))])]
    digests, _ = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    remaining = bus.digest_backlog()
    assert len(remaining) == 5
    assert all(row["processed_at"] is None for row in remaining)


async def test_digest_does_not_consume_items_the_message_omits(
    settings, conn, memory, bus
):
    eid = bus.enqueue("gmail", "new_email", "a@b.c", {"subject": "Keep me"})
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "Your calendar is clear.", "included_event_ids": []
    }))])]
    digests, _ = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    assert [row["id"] for row in bus.digest_backlog()] == [eid]


async def test_digest_refilters_backlog_after_suppression_rule_is_added(
    settings, conn, memory, bus
):
    eid = bus.enqueue(
        "gmail", "new_email", "noise@example.com", {"subject": "Do not surface"}
    )
    assert eid is not None
    bus.rules.add("gmail", "suppress", scope="noise@example.com")

    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "NOTHING", "included_event_ids": []
    }))])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    payload = client.calls[0]["messages"][0]["content"]
    assert "Do not surface" not in payload
    row = conn.execute(
        "SELECT disposition FROM events_queue WHERE id = ?", (eid,)
    ).fetchone()
    assert row["disposition"] == "suppressed"


async def test_restart_does_not_resurrect_morning_after_afternoon_ran(
    settings, conn, memory, bus, monkeypatch
):
    notifications: list[str] = []
    digests, _ = make_digests(settings, conn, memory, bus, [], notifications)
    calls: list[str] = []

    async def record(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(digests, "morning", lambda **_kwargs: record("morning"))
    monkeypatch.setattr(digests, "afternoon", lambda **_kwargs: record("afternoon"))
    monkeypatch.setattr(digests, "evening", lambda **_kwargs: record("evening"))
    today = "2026-07-22"
    kv_set(conn, "digest.last_run.afternoon", today)

    caught = await digests.catch_up(
        datetime(2026, 7, 22, 18, 46, tzinfo=ZoneInfo(settings.timezone))
    )

    assert caught is None
    assert calls == []


async def test_restart_catches_only_latest_due_weekday_brief(
    settings, conn, memory, bus, monkeypatch
):
    notifications: list[str] = []
    digests, _ = make_digests(settings, conn, memory, bus, [], notifications)
    calls: list[str] = []

    async def record(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(digests, "morning", lambda **_kwargs: record("morning"))
    monkeypatch.setattr(digests, "afternoon", lambda **_kwargs: record("afternoon"))
    monkeypatch.setattr(digests, "evening", lambda **_kwargs: record("evening"))

    caught = await digests.catch_up(
        datetime(2026, 7, 22, 18, 46, tzinfo=ZoneInfo(settings.timezone))
    )

    assert caught == "afternoon"
    assert calls == ["afternoon"]


async def test_restart_on_weekend_only_catches_evening(
    settings, conn, memory, bus, monkeypatch
):
    notifications: list[str] = []
    digests, _ = make_digests(settings, conn, memory, bus, [], notifications)
    calls: list[str] = []

    async def record(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(digests, "morning", lambda **_kwargs: record("morning"))
    monkeypatch.setattr(digests, "afternoon", lambda **_kwargs: record("afternoon"))
    monkeypatch.setattr(digests, "evening", lambda **_kwargs: record("evening"))

    caught = await digests.catch_up(
        datetime(2026, 7, 25, 22, 0, tzinfo=ZoneInfo(settings.timezone))
    )

    assert caught == "evening"
    assert calls == ["evening"]


async def test_restart_skips_old_brief_when_next_slot_is_nearly_due(
    settings, conn, memory, bus, monkeypatch
):
    notifications: list[str] = []
    digests, _ = make_digests(settings, conn, memory, bus, [], notifications)
    calls: list[str] = []

    async def record(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(digests, "morning", lambda **_kwargs: record("morning"))
    monkeypatch.setattr(digests, "afternoon", lambda **_kwargs: record("afternoon"))
    monkeypatch.setattr(digests, "evening", lambda **_kwargs: record("evening"))

    caught = await digests.catch_up(
        datetime(2026, 7, 22, 13, 45, tzinfo=ZoneInfo(settings.timezone))
    )

    assert caught is None
    assert calls == []


async def test_restart_catchup_prompt_is_current_and_not_slot_theatre(
    settings, conn, memory, bus
):
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Google Chat — Reanne's brief is in.")])]
    digests, client = make_digests(
        settings, conn, memory, bus, script, notifications
    )

    caught = await digests.catch_up(
        datetime(2026, 7, 22, 18, 46, tzinfo=ZoneInfo(settings.timezone))
    )

    system_text = "\n".join(
        block["text"] for block in client.calls[0]["system"] if "text" in block
    )
    assert caught == "afternoon"
    assert "The local time is Wednesday 18:46" in system_text
    assert "Never mention a restart, catch-up" in system_text
    assert notifications == ["Google Chat — Reanne's brief is in."]


async def test_failed_restart_catchup_is_left_unmarked_for_retry(
    settings, conn, memory, bus
):
    notifications: list[str] = []
    digests, _ = make_digests(
        settings,
        conn,
        memory,
        bus,
        [RuntimeError("provider unavailable")],
        notifications,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await digests.catch_up(
            datetime(2026, 7, 22, 10, 0, tzinfo=ZoneInfo(settings.timezone))
        )

    assert kv_get(conn, "digest.last_run.morning") is None
