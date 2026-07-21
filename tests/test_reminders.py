"""Reminders: persistence, zero-token firing, rehydration after restart."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from samantha.reminders import ReminderService

TZ = "Asia/Kuala_Lumpur"


@pytest.fixture
async def scheduler():
    s = AsyncIOScheduler(timezone=TZ, event_loop=asyncio.get_running_loop())
    s.start()
    yield s
    s.shutdown(wait=False)


async def test_reminder_fires_with_zero_llm_tokens(conn, scheduler):
    delivered: list[tuple[int, str]] = []

    async def deliver(rid: int, text: str) -> None:
        delivered.append((rid, text))

    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)
    due = datetime.now(timezone.utc) + timedelta(seconds=0.2)
    rid = svc.set("Call mum!", due)

    await asyncio.sleep(0.8)

    assert delivered == [(rid, "Call mum!")]
    row = conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "fired"
    # The zero-token guarantee: nothing hit the spend ledger.
    count = conn.execute("SELECT COUNT(*) AS c FROM spend_log").fetchone()["c"]
    assert count == 0


async def test_reminders_survive_restart(conn, scheduler):
    svc1 = ReminderService(conn, scheduler, TZ, deliver=None)
    rid = svc1.set("Future thing", datetime.now(timezone.utc) + timedelta(hours=2))
    # Simulate restart: fresh scheduler + service over the same DB.
    scheduler.remove_all_jobs()
    assert scheduler.get_job(f"reminder-{rid}") is None

    svc2 = ReminderService(conn, scheduler, TZ, deliver=None)
    n = svc2.rehydrate()
    assert n == 1
    assert scheduler.get_job(f"reminder-{rid}") is not None


async def test_overdue_reminder_fires_on_rehydrate(conn, scheduler):
    delivered: list[str] = []

    async def deliver(_rid: int, text: str) -> None:
        delivered.append(text)

    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)
    conn.execute(
        "INSERT INTO reminders(text, due_at) VALUES (?, ?)",
        ("Missed me", (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()),
    )
    conn.commit()
    svc.rehydrate()
    await asyncio.sleep(0.5)
    assert delivered and "overdue" in delivered[0]


async def test_cancel_removes_job_and_marks_row(conn, scheduler):
    svc = ReminderService(conn, scheduler, TZ, deliver=None)
    rid = svc.set("Nope", datetime.now(timezone.utc) + timedelta(hours=1))
    assert svc.cancel(rid) is True
    assert scheduler.get_job(f"reminder-{rid}") is None
    row = conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "cancelled"
    assert svc.cancel(rid) is False  # idempotent


async def test_recurring_reminder_stays_scheduled_after_fire(conn, scheduler):
    svc = ReminderService(conn, scheduler, TZ, deliver=None)
    rid = svc.set("Weekly review", datetime.now(timezone.utc) + timedelta(days=1), recurrence="0 9 * * 1")
    await svc._fire(rid, overdue=False)
    row = conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "scheduled"  # recurring: never consumed


async def test_snooze_reschedules(conn, scheduler):
    svc = ReminderService(conn, scheduler, TZ, deliver=None)
    rid = svc.set("Snoozable", datetime.now(timezone.utc) + timedelta(seconds=1))
    new_due = svc.snooze(rid, 60)
    assert new_due is not None
    job = scheduler.get_job(f"reminder-{rid}")
    assert job is not None
