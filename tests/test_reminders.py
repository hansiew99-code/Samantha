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
    async def deliver(_rid: int, _text: str) -> None:
        return None

    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)
    rid = svc.set(
        "Weekly review",
        datetime.now(timezone.utc) + timedelta(days=1),
        recurrence="0 9 * * 0",
    )
    await svc._fire(rid, overdue=False)
    row = conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "scheduled"  # recurring: never consumed


async def test_recurring_fire_persists_next_occurrence_for_restart(conn, scheduler):
    async def deliver(_rid: int, _text: str) -> None:
        return None

    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)
    rid = svc.set(
        "Daily review",
        datetime.now(timezone.utc) + timedelta(minutes=1),
        recurrence="* * * * *",
    )
    # Model APScheduler's state while a cron occurrence is executing: the
    # durable row still names the occurrence being delivered, while the job
    # has already advanced to its following run.
    stale_due = datetime.now(timezone.utc) - timedelta(seconds=1)
    next_due = datetime.now(timezone.utc) + timedelta(minutes=2)
    conn.execute(
        "UPDATE reminders SET due_at = ? WHERE id = ?",
        (stale_due.isoformat(), rid),
    )
    conn.commit()
    scheduler.modify_job(f"reminder-{rid}", next_run_time=next_due)

    await svc._fire(rid, overdue=False)

    stored = datetime.fromisoformat(conn.execute(
        "SELECT due_at FROM reminders WHERE id = ?", (rid,)
    ).fetchone()["due_at"])
    assert abs((stored - next_due).total_seconds()) < 0.01

    scheduler.remove_all_jobs()
    delivered: list[str] = []

    async def restarted_delivery(_rid: int, text: str) -> None:
        delivered.append(text)

    restarted = ReminderService(conn, scheduler, TZ, deliver=restarted_delivery)
    assert restarted.rehydrate() == 1
    await asyncio.sleep(0.1)
    assert delivered == []  # no false overdue replay after a normal fire


async def test_snooze_reschedules(conn, scheduler):
    svc = ReminderService(conn, scheduler, TZ, deliver=None)
    rid = svc.set("Snoozable", datetime.now(timezone.utc) + timedelta(seconds=1))
    new_due = svc.snooze(rid, 60)
    assert new_due is not None
    job = scheduler.get_job(f"reminder-{rid}-snooze")
    assert job is not None


async def test_failed_delivery_stays_pending_and_retries(conn, scheduler):
    attempts = 0

    async def flaky_delivery(_rid: int, _text: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("telegram temporarily unavailable")

    svc = ReminderService(conn, scheduler, TZ, deliver=flaky_delivery)
    rid = svc.set("Don't lose me", datetime.now(timezone.utc) + timedelta(hours=1))

    await svc._fire(rid, overdue=False)

    row = conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "scheduled"
    assert scheduler.get_job(f"reminder-{rid}-delivery-retry") is not None

    await svc._fire(rid, overdue=False)

    row = conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "fired"
    assert attempts == 2
    assert scheduler.get_job(f"reminder-{rid}-delivery-retry") is None


async def test_rehydrate_recovers_interrupted_delivery(conn, scheduler):
    due = datetime.now(timezone.utc) + timedelta(hours=1)
    rid = conn.execute(
        "INSERT INTO reminders(text, due_at, status) VALUES (?, ?, 'delivering')",
        ("Claimed before a crash", due.isoformat()),
    ).lastrowid
    conn.commit()

    svc = ReminderService(conn, scheduler, TZ, deliver=None)
    assert svc.rehydrate() == 1

    row = conn.execute("SELECT status FROM reminders WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "scheduled"
    assert scheduler.get_job(f"reminder-{rid}") is not None


async def test_snoozed_recurring_reminder_restores_its_cron(conn, scheduler):
    delivered: list[str] = []

    async def deliver(_rid: int, text: str) -> None:
        delivered.append(text)

    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)
    rid = svc.set(
        "Weekly review",
        datetime.now(timezone.utc) + timedelta(days=1),
        recurrence="0 9 * * 0",
    )
    svc.snooze(rid, 60)
    assert scheduler.get_job(f"reminder-{rid}") is None
    assert scheduler.get_job(f"reminder-{rid}-snooze") is not None

    await svc._fire(rid, overdue=False)

    assert delivered == ["Weekly review"]
    assert scheduler.get_job(f"reminder-{rid}") is not None
    assert scheduler.get_job(f"reminder-{rid}-snooze") is None
    assert conn.execute(
        "SELECT status FROM reminders WHERE id = ?", (rid,)
    ).fetchone()[0] == "scheduled"


async def test_done_for_now_preserves_recurring_series(conn, scheduler):
    svc = ReminderService(conn, scheduler, TZ, deliver=None)
    rid = svc.set(
        "Weekly review",
        datetime.now(timezone.utc) + timedelta(days=1),
        recurrence="0 9 * * 0",
    )

    assert svc.mark_done(rid) is True

    assert conn.execute(
        "SELECT status FROM reminders WHERE id = ?", (rid,)
    ).fetchone()[0] == "scheduled"
    assert scheduler.get_job(f"reminder-{rid}") is not None


async def test_recurring_reminder_catches_up_once_after_outage(conn, scheduler):
    delivered: list[str] = []

    async def deliver(_rid: int, text: str) -> None:
        delivered.append(text)

    rid = conn.execute(
        "INSERT INTO reminders(text, due_at, recurrence) VALUES (?, ?, ?)",
        (
            "Daily check",
            (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "0 9 * * *",
        ),
    ).lastrowid
    conn.commit()
    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)

    assert svc.rehydrate() == 1
    await asyncio.sleep(0.2)

    assert delivered == ["(overdue — I was offline) Daily check"]
    assert scheduler.get_job(f"reminder-{rid}") is not None


async def test_invalid_recurrence_is_rejected_before_persistence(conn, scheduler):
    svc = ReminderService(conn, scheduler, TZ, deliver=None)

    with pytest.raises(ValueError, match="5-field"):
        svc.set(
            "Bad recurrence",
            datetime.now(timezone.utc) + timedelta(hours=1),
            recurrence="daily",
        )

    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


async def test_bad_legacy_recurrence_cannot_block_restart(conn, scheduler):
    bad_id = conn.execute(
        "INSERT INTO reminders(text, due_at, recurrence) VALUES (?, ?, ?)",
        (
            "Bad legacy row",
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "not a valid cron",
        ),
    ).lastrowid
    good_id = conn.execute(
        "INSERT INTO reminders(text, due_at) VALUES (?, ?)",
        (
            "Still load me",
            (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        ),
    ).lastrowid
    conn.commit()

    svc = ReminderService(conn, scheduler, TZ, deliver=None)

    assert svc.rehydrate() == 1
    assert conn.execute(
        "SELECT status FROM reminders WHERE id = ?", (bad_id,)
    ).fetchone()["status"] == "invalid"
    assert scheduler.get_job(f"reminder-{good_id}") is not None


async def test_stale_snooze_button_cannot_resurrect_done_one_shot(conn, scheduler):
    async def deliver(_rid: int, _text: str) -> None:
        return None

    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)
    rid = svc.set("One occurrence", datetime.now(timezone.utc) + timedelta(hours=1))
    await svc._fire(rid, overdue=False)
    version = conn.execute(
        "SELECT delivery_version FROM reminders WHERE id = ?", (rid,)
    ).fetchone()["delivery_version"]

    assert svc.mark_done(rid, expected_version=version) is False
    assert svc.snooze(rid, 60, expected_version=version) is None
    row = conn.execute(
        "SELECT status FROM reminders WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "done"


async def test_stale_buttons_cannot_resurrect_cancelled_recurring_series(
    conn, scheduler
):
    async def deliver(_rid: int, _text: str) -> None:
        return None

    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)
    rid = svc.set(
        "Daily series",
        datetime.now(timezone.utc) + timedelta(hours=1),
        recurrence="0 9 * * *",
    )
    await svc._fire(rid, overdue=False)
    version = conn.execute(
        "SELECT delivery_version FROM reminders WHERE id = ?", (rid,)
    ).fetchone()["delivery_version"]

    assert svc.cancel(rid) is True
    assert svc.mark_done(rid, expected_version=version) is None
    assert svc.snooze(rid, 60, expected_version=version) is None
    assert conn.execute(
        "SELECT status FROM reminders WHERE id = ?", (rid,)
    ).fetchone()["status"] == "cancelled"


async def test_second_button_tap_cannot_change_acknowledged_recurring_occurrence(
    conn, scheduler
):
    async def deliver(_rid: int, _text: str) -> None:
        return None

    svc = ReminderService(conn, scheduler, TZ, deliver=deliver)
    rid = svc.set(
        "Daily series",
        datetime.now(timezone.utc) + timedelta(hours=1),
        recurrence="0 9 * * *",
    )
    await svc._fire(rid, overdue=False)
    version = conn.execute(
        "SELECT delivery_version FROM reminders WHERE id = ?", (rid,)
    ).fetchone()["delivery_version"]

    assert svc.mark_done(rid, expected_version=version) is True
    due_after_done = conn.execute(
        "SELECT due_at FROM reminders WHERE id = ?", (rid,)
    ).fetchone()["due_at"]
    assert svc.snooze(rid, 60, expected_version=version) is None
    row = conn.execute(
        "SELECT status, due_at FROM reminders WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "scheduled"
    assert row["due_at"] == due_after_done


async def test_failed_cancel_racing_inflight_fire_cannot_orphan_recurring_job(
    conn, scheduler
):
    svc = ReminderService(conn, scheduler, TZ, deliver=None)
    rid = svc.set(
        "Daily series",
        datetime.now(timezone.utc) + timedelta(hours=1),
        recurrence="0 9 * * *",
    )
    assert scheduler.get_job(f"reminder-{rid}") is not None
    conn.execute(
        "UPDATE reminders SET status = 'delivering_scheduled' WHERE id = ?",
        (rid,),
    )
    conn.commit()

    assert svc.cancel(rid) is False
    assert scheduler.get_job(f"reminder-{rid}") is not None
