"""Reminder continuity: resolve safe aliases and confirm the actual schedule."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from fakes import FakeAnthropicClient, FakeResponse, text_block, tool_use
from samantha.brain import Brain
from samantha.governor import Governor
from samantha.reminders import ReminderService
from samantha.tools import Tool, ToolRegistry
from samantha.tools import reminder_tools


TZ = "Asia/Kuala_Lumpur"


@pytest.fixture
async def scheduler():
    value = AsyncIOScheduler(timezone=TZ, event_loop=asyncio.get_running_loop())
    value.start()
    yield value
    value.shutdown(wait=False)


def _future_meeting() -> datetime:
    """Return a stable future 2 pm in the owner's timezone."""
    local = datetime.now(ZoneInfo(TZ)) + timedelta(days=1)
    return local.replace(hour=14, minute=0, second=0, microsecond=0)


def _brain(settings, memory, conn, scheduler, script, calendar_calls=None):
    registry = ToolRegistry()
    if calendar_calls is not None:
        meeting = _future_meeting()
        registry.register(Tool(
            name="calendar_list_events",
            description="List current Calendar events.",
            input_schema={
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
                "required": ["start", "end"],
            },
            func=lambda start, end: calendar_calls.append((start, end))
            or f"[evt-phillip] {meeting.isoformat()} → "
            f"{(meeting + timedelta(hours=1)).isoformat()}: "
            "Ad feedback with Phillip",
        ))
    reminder_tools.register(
        registry,
        ReminderService(conn, scheduler, TZ, deliver=None),
    )
    client = FakeAnthropicClient(script)
    return (
        Brain(
            settings,
            memory,
            registry,
            Governor(conn, settings.daily_budget_usd),
            client=client,
        ),
        client,
    )


async def test_unique_phil_alias_recovers_without_reasking_and_never_says_done(
    settings, memory, conn, scheduler
):
    meeting = _future_meeting()
    due = meeting - timedelta(minutes=10)
    memory.log_message(
        "assistant",
        f"Tomorrow: Phillip at {meeting:%-I%p}. Reanne's brief is also in.",
        channel="telegram_push",
    )
    script = [
        # Reproduce the weak production response. Brain should correct it once
        # by doing the live Calendar read the owner should not have to request.
        FakeResponse(content=[text_block("When's the meeting with Phil?")]),
        FakeResponse(
            content=[
                tool_use(
                    "calendar_list_events",
                    {
                        "start": meeting.date().isoformat(),
                        "end": (meeting + timedelta(days=7)).date().isoformat(),
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(
            content=[
                tool_use(
                    "reminders_set",
                    {
                        "text": "Ask Phil about the Lazada spend",
                        "due_at": due.isoformat(),
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        # Reproduce the second weak production response. The deterministic
        # receipt must replace it with the useful what + when confirmation.
        FakeResponse(content=[text_block("Done.")]),
    ]
    calendar_calls: list[tuple[str, str]] = []
    brain, client = _brain(
        settings, memory, conn, scheduler, script, calendar_calls
    )

    reply = await brain.handle_message(
        "Remind me before the meeting tomorrow to ask Phil about the Lazada spend."
    )

    assert reply != "Done."
    assert "Ask Phil about the Lazada spend" in reply
    assert "tomorrow at 1:50 pm" in reply
    assert "1:50 pm" in reply
    assert len(client.calls) == 4
    assert calendar_calls
    first_system = "\n".join(block["text"] for block in client.calls[0]["system"])
    assert "call calendar_list_events" in first_system
    assert "phil/philip/phillip" in first_system.casefold()
    assert "use 10 minutes before" in first_system
    corrected_system = "\n".join(
        block["text"] for block in client.calls[1]["system"]
    )
    assert "call calendar_list_events" in corrected_system

    row = conn.execute(
        "SELECT text, due_at FROM reminders ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["text"] == "Ask Phil about the Lazada spend"
    assert datetime.fromisoformat(row["due_at"]) == due.astimezone(ZoneInfo("UTC"))


@pytest.mark.parametrize(
    "reply",
    ["Done.", "All set ✓", "Done — reminder's set.", "The reminder is scheduled."],
)
def test_low_information_reminder_confirmations_are_detected(reply):
    assert Brain._is_low_information_confirmation(reply)


async def test_old_prose_never_proves_a_meeting_date(
    settings, memory, conn, scheduler
):
    memory.log_message(
        "assistant",
        "Tomorrow: Phillip at 2pm; Friday: Phillip at 4pm.",
        channel="telegram_push",
    )
    brain, _client = _brain(settings, memory, conn, scheduler, [], [])

    guidance, resolved = brain._reminder_context_guidance(
        "Remind me before the meeting to ask Phil about spend."
    )

    assert resolved is True
    assert "calendar_list_events" in guidance
    assert "prose from an older conversation is not proof" in guidance


async def test_exact_digest_sentence_requires_live_calendar_not_nearest_clock(
    settings, memory, conn, scheduler
):
    memory.log_message(
        "assistant",
        "Tomorrow's packed. Creative check-in at 9:30am, then ad feedback "
        "with Phillip at 2pm (Gmail).",
        channel="telegram_push",
    )
    brain, _client = _brain(settings, memory, conn, scheduler, [], [])

    guidance, resolved = brain._reminder_context_guidance(
        "Remind me before the meeting to ask Phil about the Lazada spend."
    )

    assert resolved is True
    assert "calendar_list_events" in guidance
    assert "2pm" not in guidance
    assert "9:30" not in guidance


async def test_past_phillip_prose_still_requires_live_calendar(
    settings, memory, conn, scheduler
):
    memory.log_message(
        "assistant",
        "Yesterday: Phillip was at 2pm. Sarah is tomorrow at 3pm.",
        channel="telegram_push",
    )
    brain, _client = _brain(settings, memory, conn, scheduler, [], [])

    guidance, resolved = brain._reminder_context_guidance(
        "Remind me before the meeting to ask Phil about spend."
    )

    assert resolved is True
    assert "calendar_list_events" in guidance


async def test_explicit_owner_clock_does_not_force_calendar_read(
    settings, memory, conn, scheduler
):
    brain, _client = _brain(settings, memory, conn, scheduler, [], [])

    guidance, requires_calendar = brain._reminder_context_guidance(
        "Remind me at 1:50pm tomorrow to ask Phil about spend."
    )

    assert guidance == ""
    assert requires_calendar is False


async def test_clock_without_a_date_still_requires_calendar_proof(
    settings, memory, conn, scheduler
):
    brain, _client = _brain(settings, memory, conn, scheduler, [], [])

    guidance, requires_calendar = brain._reminder_context_guidance(
        "Remind me before the 2pm meeting with Phil to ask about spend."
    )

    assert requires_calendar is True
    assert "calendar_list_events" in guidance


async def test_unrelated_deadline_clock_cannot_bypass_calendar_proof(
    settings, memory, conn, scheduler
):
    brain, _client = _brain(settings, memory, conn, scheduler, [], [])

    guidance, requires_calendar = brain._reminder_context_guidance(
        "Remind me before the meeting tomorrow to ask Phil about the 3pm deadline."
    )

    assert requires_calendar is True
    assert "calendar_list_events" in guidance


async def test_explicit_owner_fire_time_is_bound_before_reminder_mutation(
    settings, memory, conn, scheduler
):
    expected = _future_meeting() - timedelta(minutes=10)
    wrong = expected + timedelta(hours=1)
    script = [
        FakeResponse(
            content=[tool_use(
                "reminders_set",
                {"text": "Take medicine", "due_at": wrong.isoformat()},
            )],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block(
            "I'll remind you tomorrow at 2:50 pm to take medicine."
        )]),
    ]
    brain, _client = _brain(settings, memory, conn, scheduler, script)

    reply = await brain.handle_message(
        f"Remind me at {expected:%-I:%M %p} tomorrow to take medicine."
    )

    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
    assert reply == "I couldn't make that change safely, so nothing was changed."


async def test_explicit_owner_fire_time_accepts_matching_owner_local_due(
    settings, memory, conn, scheduler
):
    expected = _future_meeting() - timedelta(minutes=10)
    brain, _client = _brain(settings, memory, conn, scheduler, [], [])

    parsed = brain._explicit_reminder_due(
        f"Remind me tomorrow at {expected:%-I:%M %p} to take medicine."
    )

    assert parsed == expected


def test_date_hint_understands_named_dates_and_rejects_vague_next_week():
    now = datetime(2026, 7, 22, 22, 0, tzinfo=ZoneInfo(TZ))

    assert Brain._date_hint("the meeting on 23 July", now).isoformat() == "2026-07-23"
    assert Brain._date_hint("the meeting on 3 Jan", now).isoformat() == "2027-01-03"
    assert Brain._date_hint("the meeting next week", now) is None


async def test_informative_reminder_confirmation_is_preserved(
    settings, memory, conn, scheduler
):
    meeting = _future_meeting()
    due = meeting - timedelta(minutes=10)
    expected = "Set for tomorrow at 1:50 pm — ask Phil about the Lazada spend."
    script = [
        FakeResponse(
            content=[
                tool_use(
                    "reminders_set",
                    {
                        "text": "Ask Phil about the Lazada spend",
                        "due_at": due.isoformat(),
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block(expected)]),
    ]
    brain, _client = _brain(settings, memory, conn, scheduler, script)

    reply = await brain.handle_message(
        f"Remind me at {due:%-I:%M %p} tomorrow to ask Phil about the Lazada spend."
    )

    assert reply == expected


@pytest.mark.parametrize(
    "weak_final",
    [
        "All done.",
        "Sure — done.",
        "I've set that.",
        "Done — I'll nudge you before the meeting.",
        "That's scheduled.",
    ],
)
async def test_every_vague_reminder_final_falls_back_to_what_and_when(
    settings, memory, conn, scheduler, weak_final
):
    due = _future_meeting() - timedelta(minutes=10)
    script = [
        FakeResponse(
            content=[
                tool_use(
                    "reminders_set",
                    {
                        "text": "Ask Phil about the Lazada spend",
                        "due_at": due.isoformat(),
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block(weak_final)]),
    ]
    brain, _client = _brain(settings, memory, conn, scheduler, script)

    reply = await brain.handle_message(
        f"Remind me at {due:%-I:%M %p} tomorrow to ask Phil about the Lazada spend."
    )

    assert reply.startswith("I'll remind you tomorrow at")
    assert "Ask Phil about the Lazada spend" in reply


@pytest.mark.parametrize("subject", ["Ask Jo", "X", "向李明询问预算", "药", "👍"])
async def test_short_or_non_latin_subject_still_gets_specific_receipt(
    settings, memory, conn, scheduler, subject
):
    due = _future_meeting() - timedelta(minutes=10)
    script = [
        FakeResponse(
            content=[tool_use(
                "reminders_set",
                {"text": subject, "due_at": due.isoformat()},
            )],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block("Done.")]),
    ]
    brain, _client = _brain(settings, memory, conn, scheduler, script)

    reply = await brain.handle_message(
        f"Remind me at {due:%-I:%M %p} tomorrow: {subject}."
    )

    assert "tomorrow at 1:50 pm" in reply
    assert subject in reply


async def test_wrong_model_clock_is_replaced_by_authoritative_receipt(
    settings, memory, conn, scheduler
):
    due = _future_meeting() - timedelta(minutes=10)
    script = [
        FakeResponse(
            content=[tool_use(
                "reminders_set",
                {
                    "text": "Ask Phil about the Lazada spend",
                    "due_at": due.isoformat(),
                },
            )],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block(
            "I'll remind you tomorrow at 2pm about the Lazada spend."
        )]),
    ]
    brain, _client = _brain(settings, memory, conn, scheduler, script)

    reply = await brain.handle_message(
        f"Remind me at {due:%-I:%M %p} tomorrow to ask Phil about the Lazada spend."
    )

    assert "tomorrow at 1:50 pm" in reply
    assert "tomorrow at 2pm" not in reply


def test_calendar_proof_requires_one_matching_event_at_the_expected_start():
    meeting = _future_meeting()
    due = meeting - timedelta(minutes=10)
    requirement = {
        "alias": "phil",
        "lead_minutes": 10,
        "requested_date": meeting.date().isoformat(),
    }
    calendar_input = {
        "start": meeting.date().isoformat(),
        "end": (meeting.date() + timedelta(days=1)).isoformat(),
    }
    valid = (
        f"[evt-1] {meeting.isoformat()} → "
        f"{(meeting + timedelta(hours=1)).isoformat()}: Ad feedback with Phillip"
    )

    assert Brain._calendar_supports_reminder(
        valid,
        calendar_input,
        {"due_at": due.isoformat()},
        requirement,
        TZ,
    )
    assert not Brain._calendar_supports_reminder(
        "No events in that window.",
        calendar_input,
        {"due_at": due.isoformat()},
        requirement,
        TZ,
    )
    assert not Brain._calendar_supports_reminder(
        valid,
        calendar_input,
        {"due_at": meeting.isoformat()},
        requirement,
        TZ,
    )
    assert not Brain._calendar_supports_reminder(
        valid + "\n" + valid.replace("evt-1", "evt-2"),
        calendar_input,
        {"due_at": due.isoformat()},
        requirement,
        TZ,
    )


def test_calendar_proof_rejects_wrong_day_past_event_and_partial_search():
    meeting = _future_meeting()
    due = meeting - timedelta(minutes=10)
    requirement = {
        "alias": "phil",
        "lead_minutes": 10,
        "requested_date": meeting.date().isoformat(),
    }
    next_day = meeting + timedelta(days=1)
    wrong_day = (
        f"[evt-next] {next_day.isoformat()} → "
        f"{(next_day + timedelta(hours=1)).isoformat()}: Phillip review"
    )
    full_day_query = {
        "start": meeting.date().isoformat(),
        "end": (meeting.date() + timedelta(days=1)).isoformat(),
    }

    assert not Brain._calendar_supports_reminder(
        wrong_day,
        full_day_query,
        {"due_at": due.isoformat()},
        requirement,
        TZ,
    )

    past = datetime.now(ZoneInfo(TZ)) - timedelta(hours=2)
    past_requirement = {
        **requirement,
        "requested_date": past.date().isoformat(),
    }
    past_query = {
        "start": past.date().isoformat(),
        "end": (past.date() + timedelta(days=1)).isoformat(),
    }
    past_result = (
        f"[evt-past] {past.isoformat()} → "
        f"{(past + timedelta(hours=1)).isoformat()}: Phillip review"
    )
    assert not Brain._calendar_supports_reminder(
        past_result,
        past_query,
        {"due_at": (past - timedelta(minutes=10)).isoformat()},
        past_requirement,
        TZ,
    )

    valid = (
        f"[evt-1] {meeting.isoformat()} → "
        f"{(meeting + timedelta(hours=1)).isoformat()}: Phillip review"
    )
    partial_query = {
        "start": meeting.replace(hour=9).isoformat(),
        "end": meeting.replace(hour=17).isoformat(),
    }
    assert not Brain._calendar_supports_reminder(
        valid,
        partial_query,
        {"due_at": due.isoformat()},
        requirement,
        TZ,
    )


def test_calendar_proof_rejects_truncated_or_under_scoped_undated_results():
    meeting = _future_meeting()
    due = meeting - timedelta(minutes=10)
    valid = (
        f"[evt-1] {meeting.isoformat()} → "
        f"{(meeting + timedelta(hours=1)).isoformat()}: Phillip review"
    )
    requirement = {"alias": "phil", "lead_minutes": 10, "requested_date": None}
    owner_now = datetime.now(ZoneInfo(TZ))
    full_query = {
        "start": (owner_now - timedelta(minutes=1)).isoformat(),
        "end": (owner_now + timedelta(days=14, minutes=1)).isoformat(),
    }

    assert not Brain._calendar_supports_reminder(
        valid + "\n[More than 20 events match; narrow the date window for the rest.]",
        full_query,
        {"due_at": due.isoformat()},
        requirement,
        TZ,
    )
    assert not Brain._calendar_supports_reminder(
        valid,
        {
            "start": (meeting - timedelta(hours=1)).isoformat(),
            "end": (meeting + timedelta(hours=1)).isoformat(),
        },
        {"due_at": due.isoformat()},
        requirement,
        TZ,
    )
    assert Brain._calendar_supports_reminder(
        valid,
        full_query,
        {"due_at": due.isoformat()},
        requirement,
        TZ,
    )


def test_human_due_treats_naive_iso_as_owner_local_time():
    owner_tz = ZoneInfo(TZ)
    # 18:00 UTC is already 02:00 the next day in Kuala Lumpur. A naive 01:50
    # input is therefore "today" for the owner, not "tomorrow" by VM time.
    utc_now = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)
    naive_owner_due = datetime(2026, 7, 23, 1, 50)

    rendered = reminder_tools._human_due(
        naive_owner_due,
        owner_tz,
        now=utc_now,
    )

    assert rendered == "today at 1:50 am"
