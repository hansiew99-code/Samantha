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
DELIVERY_RETRY_DELAY = timedelta(minutes=2)


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
        # Validate recurrence before the durable insert.  A malformed cron row
        # with status=scheduled but no APScheduler job would otherwise survive
        # forever and could abort every future startup rehydration.
        if recurrence:
            self._cron_trigger(recurrence, due_utc)
        existing = self.conn.execute(
            """SELECT id FROM reminders
               WHERE text = ? AND due_at = ? AND recurrence IS ?
                 AND status IN ('scheduled','snoozed')
               ORDER BY id DESC LIMIT 1""",
            (text, due_utc.isoformat(), recurrence),
        ).fetchone()
        if existing is not None:
            rid = int(existing["id"])
            self._schedule(rid, due_utc, recurrence)
            return rid
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
        if cur.rowcount > 0:
            self._remove_job(reminder_id)
        return cur.rowcount > 0

    def snooze(
        self,
        reminder_id: int,
        minutes: int,
        *,
        expected_version: int | None = None,
    ) -> datetime | None:
        row = self.conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if (
            row is None
            or row["status"] not in ("scheduled", "snoozed", "fired")
            or (
                expected_version is not None
                and int(row["delivery_version"]) != expected_version
            )
        ):
            return None
        new_due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        params: list[object] = [new_due.isoformat(), reminder_id]
        version_clause = ""
        if expected_version is not None:
            version_clause = " AND delivery_version = ?"
            params.append(expected_version)
        changed = self.conn.execute(
            "UPDATE reminders SET due_at = ?, status = 'snoozed', "
            "delivery_version = delivery_version + 1 "
            "WHERE id = ? AND status IN ('scheduled','snoozed','fired')"
            + version_clause,
            params,
        )
        self.conn.commit()
        if changed.rowcount == 0:
            return None
        # Pause a recurring cron for this occurrence, then restore it after the
        # snoozed delivery.  A distinct one-shot id prevents orphaning the cron.
        self._remove_job(reminder_id)
        self._schedule_snooze(reminder_id, new_due)
        return new_due

    def mark_done(
        self, reminder_id: int, *, expected_version: int | None = None
    ) -> bool | None:
        """Acknowledge one occurrence.

        Returns True for a recurring series, False for a one-shot, and None
        when a stale/terminal Telegram button must not change anything.
        """
        row = self.conn.execute(
            "SELECT recurrence, due_at, status, delivery_version FROM reminders "
            "WHERE id = ?",
            (reminder_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] not in ("scheduled", "snoozed", "fired")
            or (
                expected_version is not None
                and int(row["delivery_version"]) != expected_version
            )
        ):
            return None
        version_clause = ""
        params: list[object] = [reminder_id]
        if expected_version is not None:
            version_clause = " AND delivery_version = ?"
            params.append(expected_version)
        if row is not None and row["recurrence"]:
            changed = self.conn.execute(
                "UPDATE reminders SET status = 'scheduled', "
                "delivery_version = delivery_version + 1 "
                "WHERE id = ? AND status IN ('scheduled','snoozed','fired')"
                + version_clause,
                params,
            )
            self.conn.commit()
            if changed.rowcount == 0:
                return None
            self._remove_job(reminder_id)
            self._schedule(
                reminder_id,
                max(datetime.now(timezone.utc), datetime.fromisoformat(row["due_at"])),
                row["recurrence"],
            )
            return True
        changed = self.conn.execute(
            "UPDATE reminders SET status = 'done', "
            "delivery_version = delivery_version + 1 "
            "WHERE id = ? AND status IN ('scheduled','snoozed','fired')"
            + version_clause,
            params,
        )
        self.conn.commit()
        if changed.rowcount == 0:
            return None
        self._remove_job(reminder_id)
        return False

    def list_upcoming(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE status IN ('scheduled','snoozed') "
            "ORDER BY due_at LIMIT ?",
            (limit,),
        ).fetchall()

    # -- scheduling ----------------------------------------------------------

    def rehydrate(self) -> int:
        """On boot: reschedule everything pending. Past-due one-shots fire
        immediately (marked overdue) rather than being lost.  A process can
        die after claiming a reminder but before Telegram accepts it, so any
        orphaned ``delivering`` state is returned to the pending queue first."""
        self.conn.execute(
            "UPDATE reminders SET status = 'scheduled' "
            "WHERE status IN ('delivering', 'delivering_scheduled')"
        )
        self.conn.execute(
            "UPDATE reminders SET status = 'snoozed' "
            "WHERE status = 'delivering_snoozed'"
        )
        self.conn.commit()
        count = 0
        now = datetime.now(timezone.utc)
        for row in self.list_upcoming(limit=10_000):
            rid = int(row["id"])
            try:
                due = datetime.fromisoformat(row["due_at"])
                if row["status"] == "snoozed":
                    self._schedule_snooze(rid, max(due, now), overdue=due <= now)
                elif row["recurrence"]:
                    missed = due <= now
                    self._schedule(rid, max(due, now), row["recurrence"])
                    if missed:
                        # Deliver one catch-up occurrence, never a burst for every
                        # cron tick elapsed while the host was down.
                        self._schedule_snooze(rid, now, overdue=True)
                elif due <= now:
                    self.scheduler.add_job(
                        self._fire, args=[rid, True], id=self._job_id(rid),
                        replace_existing=True,
                    )
                else:
                    self._schedule(rid, due, None)
            except (TypeError, ValueError):
                # Quarantine bad legacy/manual rows without allowing one bad
                # commitment to brick the entire daemon on every restart.
                self.conn.execute(
                    "UPDATE reminders SET status = 'invalid' WHERE id = ?", (rid,)
                )
                self.conn.commit()
                log.exception("invalid reminder %d quarantined during rehydrate", rid)
                continue
            except Exception:
                # An operational scheduler error should not mislabel the row as
                # invalid, but it also must not prevent other reminders loading.
                log.exception("reminder %d could not be rehydrated", rid)
                continue
            count += 1
        log.info("rehydrated %d reminders", count)
        return count

    def _schedule(self, rid: int, due_utc: datetime, recurrence: str | None) -> None:
        if recurrence:
            trigger = self._cron_trigger(recurrence, due_utc)
            job = self.scheduler.add_job(
                self._fire, trigger, args=[rid, False], id=self._job_id(rid),
                replace_existing=True, misfire_grace_time=3600,
            )
            if job.next_run_time is not None:
                self.conn.execute(
                    "UPDATE reminders SET due_at = ? WHERE id = ?",
                    (job.next_run_time.astimezone(timezone.utc).isoformat(), rid),
                )
                self.conn.commit()
        else:
            self.scheduler.add_job(
                self._fire, "date", run_date=due_utc, args=[rid, False],
                id=self._job_id(rid), replace_existing=True, misfire_grace_time=3600,
            )

    def _cron_trigger(self, recurrence: str, due_utc: datetime) -> CronTrigger:
        fields = recurrence.split()
        if len(fields) != 5:
            raise ValueError("recurrence must be a 5-field APScheduler cron expression")
        minute, hour, day, month, day_of_week = fields
        try:
            return CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
                timezone=self.tz,
                start_date=due_utc,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid recurrence cron: {recurrence!r}") from exc

    def _schedule_snooze(
        self, rid: int, due_utc: datetime, *, overdue: bool = False
    ) -> None:
        self.scheduler.add_job(
            self._fire,
            "date",
            run_date=due_utc,
            args=[rid, overdue],
            id=self._snooze_job_id(rid),
            replace_existing=True,
            misfire_grace_time=3600,
        )

    def _remove_job(self, rid: int) -> None:
        for job_id in (
            self._job_id(rid),
            self._snooze_job_id(rid),
            self._retry_job_id(rid),
        ):
            job = self.scheduler.get_job(job_id)
            if job:
                job.remove()

    @staticmethod
    def _job_id(rid: int) -> str:
        return f"reminder-{rid}"

    @staticmethod
    def _retry_job_id(rid: int) -> str:
        return f"reminder-{rid}-delivery-retry"

    @staticmethod
    def _snooze_job_id(rid: int) -> str:
        return f"reminder-{rid}-snooze"

    def _schedule_retry(self, rid: int) -> None:
        self.scheduler.add_job(
            self._fire,
            "date",
            run_date=datetime.now(timezone.utc) + DELIVERY_RETRY_DELAY,
            args=[rid, False],
            id=self._retry_job_id(rid),
            replace_existing=True,
            misfire_grace_time=3600,
        )

    def _remove_retry_job(self, rid: int) -> None:
        job = self.scheduler.get_job(self._retry_job_id(rid))
        if job:
            job.remove()

    async def _fire(self, rid: int, overdue: bool) -> None:
        row = self.conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (rid,)
        ).fetchone()
        if row is None or row["status"] not in ("scheduled", "snoozed"):
            return
        previous_status = str(row["status"])
        delivering_status = f"delivering_{previous_status}"
        claimed = self.conn.execute(
            "UPDATE reminders SET status = ?, "
            "delivery_version = delivery_version + 1 "
            "WHERE id = ? AND status = ?",
            (delivering_status, rid, previous_status),
        )
        self.conn.commit()
        if claimed.rowcount == 0:
            return
        text = row["text"]
        if overdue:
            text = f"(overdue — I was offline) {text}"
        try:
            if self.deliver is None:
                raise RuntimeError("no reminder delivery transport configured")
            await self.deliver(rid, text)
        except Exception:
            # Delivery is at-least-once: retain the pending state and retry.
            # A duplicate after a process crash is preferable to silently
            # losing a commitment the owner trusted us to remember.
            self.conn.execute(
                "UPDATE reminders SET status = ? WHERE id = ? AND status = ?",
                (previous_status, rid, delivering_status),
            )
            self.conn.commit()
            self._schedule_retry(rid)
            log.exception("reminder %d delivery failed; retry scheduled", rid)
            return

        terminal_status = "scheduled" if row["recurrence"] else "fired"
        self.conn.execute(
            "UPDATE reminders SET status = ? WHERE id = ? AND status = ?",
            (terminal_status, rid, delivering_status),
        )
        self.conn.commit()
        if row["recurrence"]:
            self._remove_retry_job(rid)
            if previous_status == "snoozed":
                self._remove_job(rid)
                self._schedule(
                    rid,
                    datetime.now(timezone.utc),
                    row["recurrence"],
                )
            else:
                # APScheduler advances a recurring job's next_run_time before
                # invoking this coroutine.  Mirror that occurrence into the
                # durable store after successful delivery.  Without this,
                # restart rehydration sees the just-fired timestamp as overdue
                # and sends a false duplicate catch-up.
                job = self.scheduler.get_job(self._job_id(rid))
                if job is not None and job.next_run_time is not None:
                    self.conn.execute(
                        "UPDATE reminders SET due_at = ? WHERE id = ?",
                        (
                            job.next_run_time.astimezone(timezone.utc).isoformat(),
                            rid,
                        ),
                    )
                    self.conn.commit()
        else:
            self._remove_job(rid)

    def _to_utc(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(timezone.utc)
