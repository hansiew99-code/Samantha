"""Reminders: stored in SQLite, scheduled with APScheduler, delivered straight
to Telegram. Firing costs zero tokens — the message text was composed at
creation time (BRIEF §6.5).

Persistence model: the `reminders` table is the store of record; jobs are
rehydrated into the (in-memory) scheduler on every boot, so reminders survive
restarts without an APScheduler jobstore dependency.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)

Deliver = Callable[[int, str], Awaitable[None]]  # (reminder_id, text)


class ReminderService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        scheduler: BaseScheduler,
        tz: str,
        deliver: Deliver | None = None,
    ) -> None:
        self.conn = conn
        self.scheduler = scheduler
        self.tz = ZoneInfo(tz)
        self.deliver = deliver

    # -- CRUD ----------------------------------------------------------------

    def set(self, text: str, due_at: datetime, recurrence: str | None = None) -> int:
        due_utc = self._to_utc(due_at)
        rid = self.conn.execute(
            "INSERT INTO reminders(text, due_at, recurrence) VALUES (?, ?, ?)",
            (text, due_utc.isoformat(), recurrence),
        ).lastrowid
        assert rid is not None
        self.conn.commit()
        self._schedule(rid, due_utc, recurrence)
        return rid

    def cancel(self, reminder_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? "
            "AND status IN ('scheduled', 'snoozed')",
            (reminder_id,),
        )
        self.conn.commit()
        self._remove_job(reminder_id)
        return cur.rowcount > 0

    def snooze(self, reminder_id: int, minutes: int) -> datetime | None:
        row = self.conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None:
            return None
        new_due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.conn.execute(
            "UPDATE reminders SET due_at = ?, status = 'snoozed' WHERE id = ?",
            (new_due.isoformat(), reminder_id),
        )
        self.conn.commit()
        self._schedule(reminder_id, new_due, None)
        return new_due

    def mark_done(self, reminder_id: int) -> None:
        self.conn.execute(
            "UPDATE reminders SET status = 'done' WHERE id = ?", (reminder_id,)
        )
        self.conn.commit()
        self._remove_job(reminder_id)

    def list_upcoming(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE status IN ('scheduled','snoozed') "
            "ORDER BY due_at LIMIT ?",
            (limit,),
        ).fetchall()

    # -- scheduling ----------------------------------------------------------

    def rehydrate(self) -> int:
        """On boot: reschedule everything pending. Past-due one-shots fire
        immediately (marked overdue) rather than being lost."""
        count = 0
        now = datetime.now(timezone.utc)
        for row in self.list_upcoming(limit=10_000):
            rid = int(row["id"])
            due = datetime.fromisoformat(row["due_at"])
            if row["recurrence"]:
                self._schedule(rid, due, row["recurrence"])
            elif due <= now:
                self.scheduler.add_job(
                    self._fire, args=[rid, True], id=self._job_id(rid),
                    replace_existing=True,
                )
            else:
                self._schedule(rid, due, None)
            count += 1
        log.info("rehydrated %d reminders", count)
        return count

    def _schedule(self, rid: int, due_utc: datetime, recurrence: str | None) -> None:
        if recurrence:
            trigger = CronTrigger.from_crontab(recurrence, timezone=self.tz)
            self.scheduler.add_job(
                self._fire, trigger, args=[rid, False], id=self._job_id(rid),
                replace_existing=True, misfire_grace_time=3600,
            )
        else:
            self.scheduler.add_job(
                self._fire, "date", run_date=due_utc, args=[rid, False],
                id=self._job_id(rid), replace_existing=True, misfire_grace_time=3600,
            )

    def _remove_job(self, rid: int) -> None:
        job = self.scheduler.get_job(self._job_id(rid))
        if job:
            job.remove()

    @staticmethod
    def _job_id(rid: int) -> str:
        return f"reminder-{rid}"

    async def _fire(self, rid: int, overdue: bool) -> None:
        row = self.conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (rid,)
        ).fetchone()
        if row is None or row["status"] in ("cancelled", "done"):
            return
        if not row["recurrence"]:
            self.conn.execute(
                "UPDATE reminders SET status = 'fired' WHERE id = ?", (rid,)
            )
            self.conn.commit()
        text = row["text"]
        if overdue:
            text = f"(overdue — I was offline) {text}"
        if self.deliver:
            await self.deliver(rid, text)

    def _to_utc(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(timezone.utc)
