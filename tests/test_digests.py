"""Digests: one LLM call, deterministic gathering, backlog consumption."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from samantha.brain import Brain
from samantha.db import kv_get, kv_set
from samantha.digests import (
    AFTERNOON_INSTRUCTIONS,
    DIGEST_INPUT_CHAR_BUDGET,
    EVENING_INSTRUCTIONS,
    MORNING_INSTRUCTIONS,
    SEEN_ITEMS_KEY,
    DigestService,
)
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


class FakeCalendar:
    def __init__(self, results: list[dict]) -> None:
        self.results = results

    def list_events(self, _start: str, _end: str) -> list[dict]:
        return self.results


def test_brief_prompts_are_short_nonrepetitive_and_temporally_grounded():
    for instructions in (
        MORNING_INSTRUCTIONS,
        AFTERNOON_INSTRUCTIONS,
        EVENING_INSTRUCTIONS,
    ):
        lowered = instructions.casefold()
        assert "at most" in lowered
        assert "tomorrow" in lowered and "tonight" in lowered
        assert "dry humour is optional" in lowered
        assert "at most one" in lowered or "limited to one" in lowered
    assert "don't restate the same fact" in MORNING_INSTRUCTIONS.casefold()
    assert "don't re-explain the same item" in AFTERNOON_INSTRUCTIONS.casefold()


def test_delivery_shape_strips_report_heading_and_enforces_hard_cap():
    text = (
        "Three things need your attention tonight. "
        "Google Chat — the EDM slides need feedback. "
        "Facebook access still isn't fixed, so boosts can't run. "
        "The rest can wait until tomorrow."
    )

    compact, clipped = DigestService._enforce_delivery_shape(text, 16)

    assert not compact.startswith("Three things")
    assert len(compact.split()) <= 16
    assert clipped is True
    assert "\n" not in compact


@pytest.mark.parametrize(
    "text",
    [
        "Two things need attention — Reanne's brief arrived.",
        "Two things require action — Facebook access is still broken.",
        "Two things need attention, especially Facebook access, which blocks the campaign.",
        "Three things need your attention tonight.",
    ],
)
def test_delivery_shape_never_strips_substance_or_returns_silence(text):
    compact, _clipped = DigestService._enforce_delivery_shape(text, 75)

    assert compact == text


def test_delivery_shape_removes_only_a_standalone_report_preamble():
    text = (
        "Three things need your attention tonight. "
        "Google Chat — Reanne's brief arrived."
    )

    compact, clipped = DigestService._enforce_delivery_shape(text, 75)

    assert compact == "Google Chat — Reanne's brief arrived."
    assert clipped is False


@pytest.mark.parametrize(
    "text",
    [
        "- Google Chat: Reanne sent the brief.\n- Gmail: Phillip replied.",
        "1. Google Chat: Reanne sent the brief.\n2. Gmail: Phillip replied.",
    ],
)
def test_delivery_shape_turns_list_layout_into_plain_conversation(text):
    compact, _clipped = DigestService._enforce_delivery_shape(text, 75)

    assert compact == (
        "Google Chat: Reanne sent the brief. Gmail: Phillip replied."
    )
    assert not re.search(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", compact)


def test_receipts_are_grounded_in_the_text_that_survived_clipping():
    data = {
        "backlog": [
            {
                "item_key": "gmail:facebook",
                "subject": "Facebook access",
                "summary": "Facebook access blocks Ocean ads",
            },
            {
                "item_key": "gchat:slides",
                "text": "EDM slides 100 and 101",
                "summary": "EDM slides need feedback",
            },
        ]
    }
    verbose = (
        "Facebook access still blocks the Ocean ads. "
        "This sentence adds enough filler to force the lower-priority EDM slides "
        "out of the final delivery completely."
    )

    delivered, clipped = DigestService._enforce_delivery_shape(verbose, 9)
    grounded = DigestService._grounded_receipt_keys(
        delivered,
        {"gmail:facebook", "gchat:slides"},
        data,
    )

    assert clipped is True
    assert grounded == {"gmail:facebook"}


def test_receipts_require_item_specific_words_not_a_shared_generic_token():
    data = {
        "backlog": [
            {
                "item_key": "gmail:facebook",
                "subject": "Facebook access review",
                "summary": "Facebook access review blocks Ocean ads",
            },
            {
                "item_key": "gchat:slides",
                "text": "EDM review",
                "summary": "EDM review needs feedback",
            },
        ]
    }

    grounded = DigestService._grounded_receipt_keys(
        "Facebook access is still blocking the Ocean ads.",
        {"gmail:facebook", "gchat:slides"},
        data,
    )

    assert grounded == {"gmail:facebook"}


def test_one_incidental_unique_word_cannot_consume_an_omitted_item():
    data = {
        "backlog": [
            {
                "item_key": "gmail:ocean",
                "subject": "Ocean launch timing",
                "summary": "Ocean launch needs a decision tomorrow",
            },
            {
                "item_key": "gchat:quarterly",
                "text": "Quarterly review",
                "summary": "Quarterly review needs feedback",
            },
        ]
    }

    grounded = DigestService._grounded_receipt_keys(
        "I'll review the Ocean launch tomorrow.",
        {"gmail:ocean", "gchat:quarterly"},
        data,
    )

    assert grounded == {"gmail:ocean"}


def test_plain_digest_is_capped_and_receipts_match_visible_items():
    data = {
        "calendar_next_48h": [
            {"item_key": "cal:1", "start": "09:00", "summary": "One"},
            {"item_key": "cal:2", "start": "10:00", "summary": "Two"},
        ],
        "open_tasks": [
            {"item_key": "task:1", "source": "clickup", "title": "Three"},
            {"item_key": "task:2", "source": "clickup", "title": "Four"},
        ],
        "backlog": [
            {"item_key": "mail:1", "source": "gmail", "summary": "Five"},
        ],
    }

    message = DigestService._plain_digest(data)

    assert "\n" not in message
    assert " — " not in message
    assert message.count(".") <= 2
    assert DigestService._plain_digest_item_keys(data) == {
        "cal:1",
        "mail:1",
    }


def make_digests(
    settings,
    conn,
    memory,
    bus,
    script,
    notifications,
    gmail=None,
    gchat=None,
    gcal=None,
):
    client = FakeAnthropicClient(script)
    brain = Brain(settings, memory, ToolRegistry(), Governor(conn, 1.0), client=client)

    async def notify(text: str) -> None:
        notifications.append(text)

    return (
        DigestService(
            settings, memory, bus, brain, notify,
            gcal=gcal, gmail=gmail, gchat=gchat, conn=conn,
        ),
        client,
    )


async def test_digest_drops_timed_calendar_events_that_already_started(
    settings, conn, memory, bus
):
    now = datetime.now(ZoneInfo(settings.timezone))
    past = now - timedelta(hours=2)
    future = now + timedelta(hours=2)
    gcal = FakeCalendar([
        {
            "id": "past-930",
            "summary": "Creative check-in",
            "start": past.isoformat(),
            "end": (past + timedelta(minutes=30)).isoformat(),
            "location": "",
        },
        {
            "id": "future",
            "summary": "Phillip review",
            "start": future.isoformat(),
            "end": (future + timedelta(minutes=30)).isoformat(),
            "location": "",
        },
    ])
    notifications: list[str] = []
    digests, _client = make_digests(
        settings, conn, memory, bus, [], notifications, gcal=gcal
    )

    data = await digests._gather()

    summaries = {item["summary"] for item in data["calendar_next_48h"]}
    assert summaries == {"Phillip review"}
    assert all("overdue" not in item["item_key"] for item in data["calendar_next_48h"])


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
        "message": "Slack — C123 sent a ping.", "included_event_ids": [eid]
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
        "message": "Gmail — the One-time item is waiting.",
        "included_event_ids": [eid],
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
        bus.enqueue("slack", "mention", "C123", {"text": f"unique{i}"})
    included = [row["id"] for row in bus.digest_backlog()[:2]]
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "Slack — unique0 and unique1 need a look.",
        "included_event_ids": included,
    }))])]
    digests, _ = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    remaining = bus.digest_backlog()
    assert len(remaining) == 28
    assert {row["id"] for row in remaining}.isdisjoint(included)


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


async def test_item_receipt_keeps_same_unread_email_out_of_later_brief(
    settings, conn, memory, bus
):
    gmail = FakeGmail([{
        "id": "msg-1",
        "thread_id": "thread-1",
        "from": "reanne@example.com",
        "subject": "August brief",
        "snippet": "Ready for review",
        "internal_date": "1784698200000",
    }])
    notifications: list[str] = []
    script = [
        FakeResponse(content=[text_block(json.dumps({
            "message": "Gmail — Reanne's August brief is ready.",
            "included_item_keys": ["gmail:msg-1"],
            "included_event_ids": [],
        }))]),
        FakeResponse(content=[text_block(json.dumps({
            "message": "NOTHING",
            "included_item_keys": [],
            "included_event_ids": [],
        }))]),
    ]
    digests, client = make_digests(
        settings, conn, memory, bus, script, notifications, gmail=gmail
    )

    await digests.afternoon()
    await digests.evening()

    first = client.calls[0]["messages"][0]["content"]
    second = client.calls[1]["messages"][0]["content"]
    assert "August brief" in first
    assert "August brief" not in second
    assert notifications == ["Gmail — Reanne's August brief is ready."]


async def test_backlog_copy_wins_over_duplicate_live_unread_scan(
    settings, conn, memory, bus
):
    payload = {
        "id": "msg-2",
        "thread_id": "thread-2",
        "from": "reanne@example.com",
        "subject": "Same brief",
        "snippet": "Same brief is ready",
        "internal_date": "1784698200000",
    }
    event_id = bus.enqueue(
        "gmail", "new_email", "reanne@example.com", payload,
        dedupe_key="gmail:msg-2",
    )
    assert event_id is not None
    gmail = FakeGmail([payload])
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "Gmail — Reanne's brief is ready.",
        "included_item_keys": ["gmail:msg-2"],
        "included_event_ids": [event_id],
    }))])]
    digests, client = make_digests(
        settings, conn, memory, bus, script, notifications, gmail=gmail
    )

    await digests.afternoon()

    model_payload = json.loads(client.calls[0]["messages"][0]["content"])
    assert model_payload["unread_email"] == []
    assert len(model_payload["backlog"]) == 1
    assert model_payload["backlog"][0]["subject"] == "Same brief"
    assert model_payload["backlog"][0]["snippet"] == "Same brief is ready"
    assert bus.digest_backlog() == []


async def test_scheduled_reminders_do_not_bloat_or_repeat_in_briefs(
    settings, conn, memory, bus
):
    conn.execute(
        "INSERT INTO reminders(text, due_at) VALUES (?, ?)",
        ("This reminder will deliver itself", "2026-07-23T01:00:00+00:00"),
    )
    conn.commit()
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "NOTHING",
        "included_item_keys": [],
        "included_event_ids": [],
    }))])]
    digests, client = make_digests(
        settings, conn, memory, bus, script, notifications
    )

    await digests.evening()

    model_payload = json.loads(client.calls[0]["messages"][0]["content"])
    assert "upcoming_reminders" not in model_payload
    assert "This reminder will deliver itself" not in json.dumps(model_payload)


async def test_digest_supplies_only_two_clipped_recent_pushes(
    settings, conn, memory, bus
):
    memory.log_message("assistant", "old push", channel="telegram_push")
    memory.log_message("assistant", "middle push", channel="telegram_push")
    memory.log_message("assistant", "new " + "x" * 500, channel="telegram_push")
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "NOTHING",
        "included_item_keys": [],
        "included_event_ids": [],
    }))])]
    digests, client = make_digests(
        settings, conn, memory, bus, script, notifications
    )

    await digests.evening()

    model_payload = json.loads(client.calls[0]["messages"][0]["content"])
    recent = model_payload["recent_pushes"]
    assert len(recent) == 2
    assert recent[0]["text"].startswith("new ")
    assert len(recent[0]["text"]) == 360
    assert recent[1]["text"] == "middle push"
    assert all("old push" not in item["text"] for item in recent)


async def test_digest_input_has_one_aggregate_context_ceiling(
    settings, conn, memory, bus
):
    gmail = FakeGmail([
        {
            "id": f"mail-{i}",
            "thread_id": f"thread-{i}",
            "from": f"sender-{i}@example.com",
            "subject": "S" * 1_000,
            "snippet": "E" * 4_000,
        }
        for i in range(20)
    ])
    chat = FakeChat([
        {
            "name": f"spaces/a/messages/{i}",
            "space": "spaces/a",
            "sender": f"users/{i}",
            "sender_name": f"Person {i}",
            "text": "C" * 4_000,
            "create_time": "2026-07-22T08:00:00Z",
        }
        for i in range(30)
    ])
    for i in range(15):
        conn.execute(
            "INSERT INTO tasks(source, external_id, title, due_at) "
            "VALUES ('local', ?, ?, ?)",
            (f"task-{i}", "T" * 2_000, "2026-07-23"),
        )
    conn.commit()
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block(json.dumps({
        "message": "NOTHING",
        "included_item_keys": [],
        "included_event_ids": [],
    }))])]
    digests, client = make_digests(
        settings,
        conn,
        memory,
        bus,
        script,
        notifications,
        gmail=gmail,
        gchat=chat,
    )

    await digests.morning()

    payload = client.calls[0]["messages"][0]["content"]
    assert len(payload) <= DIGEST_INPUT_CHAR_BUDGET


def test_seen_item_cooldown_is_short_but_older_unresolved_item_returns(
    settings, conn, memory, bus
):
    async def notify(_text: str) -> None:
        return None

    digests = DigestService(settings, memory, bus, None, notify, conn=conn)
    local_now = datetime.now(ZoneInfo(settings.timezone))
    item = {
        "id": "msg-aging",
        "from": "reanne@example.com",
        "subject": "August brief",
    }

    recent = (local_now.astimezone(ZoneInfo("UTC")) - timedelta(hours=17)).isoformat()
    kv_set(conn, SEEN_ITEMS_KEY, json.dumps({"gmail:msg-aging": recent}))
    recent_data = {"unread_email": [dict(item)]}
    digests._key_and_filter_items(recent_data, local_now)
    assert recent_data["unread_email"] == []

    older = (local_now.astimezone(ZoneInfo("UTC")) - timedelta(hours=19)).isoformat()
    kv_set(conn, SEEN_ITEMS_KEY, json.dumps({"gmail:msg-aging": older}))
    older_data = {"unread_email": [dict(item)]}
    digests._key_and_filter_items(older_data, local_now)
    assert len(older_data["unread_email"]) == 1
    assert older_data["unread_email"][0]["previously_surfaced_at"] == older


def test_clickup_surface_key_changes_with_urgency_bucket(
    settings, conn, memory, bus
):
    async def notify(_text: str) -> None:
        return None

    digests = DigestService(settings, memory, bus, None, notify, conn=conn)
    tz = ZoneInfo(settings.timezone)
    due = "2026-07-23T06:00:00+00:00"  # 14:00 Kuala Lumpur

    keys = []
    for now in (
        datetime(2026, 7, 22, 9, 0, tzinfo=tz),
        datetime(2026, 7, 23, 9, 0, tzinfo=tz),
        datetime(2026, 7, 23, 12, 30, tzinfo=tz),
        datetime(2026, 7, 23, 15, 0, tzinfo=tz),
    ):
        timing = digests._timing_fields(due, now)
        data = {
            "open_tasks": [{
                "source": "clickup",
                "external_id": "task-1",
                "title": "Launch Ocean ads",
                "due_at": due,
                **timing,
            }]
        }
        digests._key_and_filter_items(data, now)
        keys.append(data["open_tasks"][0]["item_key"])

    assert len(set(keys)) == 4
    assert keys[0].endswith(":tomorrow")
    assert keys[1].endswith(":today")
    assert keys[2].endswith(":imminent")
    assert keys[3].endswith(":overdue")
