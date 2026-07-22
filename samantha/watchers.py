"""Persistent conditional open loops.

Reminders answer "tell me at this time".  Watchers answer "only tell me if the
real-world condition is still unresolved".  Matching and delivery are plain
code after creation: integration events are the fast path and a live source
probe at the deadline/notification is the correctness path.
"""

from __future__ import annotations

import inspect
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.base import BaseScheduler

log = logging.getLogger(__name__)

Deliver = Callable[[int, str], Awaitable[None]]
# A verifier returns matching source evidence or None.  Exceptions mean the
# source was unavailable and must never be interpreted as "not found".
Verifier = Callable[[dict], dict | None | Awaitable[dict | None]]
RETRY_DELAY = timedelta(minutes=15)


class WatcherService:
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
        self._verifiers: dict[str, Verifier] = {}

    def register_verifier(self, source: str, verifier: Verifier) -> None:
        self._verifiers[source.lower()] = verifier

    def set_email(
        self,
        *,
        description: str,
        sender_contains: str,
        expected_by: datetime,
        notify_at: datetime,
        fallback_text: str,
        subject_contains: str | None = None,
        since: datetime | None = None,
        late_text: str | None = None,
        deadline_policy: str = "by_deadline",
    ) -> int:
        if "gmail" not in self._verifiers:
            raise RuntimeError("Gmail live verification is not configured")
        if not sender_contains.strip() and not (subject_contains or "").strip():
            raise ValueError("an email watch needs a sender or subject constraint")
        if deadline_policy not in {"by_deadline", "missing_at_notify"}:
            raise ValueError("deadline_policy must be by_deadline or missing_at_notify")
        expected_utc = self._to_utc(expected_by)
        notify_utc = self._to_utc(notify_at)
        if notify_utc < expected_utc:
            raise ValueError("notify_at must be at or after expected_by")
        # Default to the owner's local day boundary.  This catches an email
        # that arrived earlier today and gives model retries a stable lower
        # bound, so an omitted `since` does not create duplicate watches.
        default_since = datetime.now(self.tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        criteria = {
            "sender_contains": sender_contains.strip(),
            "subject_contains": (subject_contains or "").strip(),
            "since": self._to_utc(since or default_since).isoformat(),
        }
        criteria_json = json.dumps(criteria, ensure_ascii=False, sort_keys=True)

        # Idempotency for model retries: the same semantic watch returns the
        # existing row instead of scheduling duplicate notifications.
        existing = self.conn.execute(
            """SELECT id, deadline_policy, expected_by, notify_at FROM watchers
               WHERE source = 'gmail' AND criteria = ? AND expected_by = ?
                 AND notify_at = ? AND status IN ('active','breached')
               ORDER BY id DESC LIMIT 1""",
            (criteria_json, expected_utc.isoformat(), notify_utc.isoformat()),
        ).fetchone()
        if existing is not None:
            watcher_id = int(existing["id"])
            # Idempotency also repairs a partially scheduled watch.  A crash or
            # scheduler failure after the database commit must not leave a
            # durable promise with no job until the next process restart.
            self._ensure_jobs(
                watcher_id,
                existing["deadline_policy"],
                datetime.fromisoformat(existing["expected_by"]),
                datetime.fromisoformat(existing["notify_at"]),
            )
            return watcher_id

        wid = self.conn.execute(
            """INSERT INTO watchers(
                   description, source, criteria, expected_by, notify_at,
                   fallback_text, late_text, deadline_policy
               ) VALUES (?, 'gmail', ?, ?, ?, ?, ?, ?)""",
            (
                description.strip(),
                criteria_json,
                expected_utc.isoformat(),
                notify_utc.isoformat(),
                fallback_text.strip(),
                (late_text or "").strip() or None,
                deadline_policy,
            ),
        ).lastrowid
        assert wid is not None
        self.conn.commit()
        self._ensure_jobs(int(wid), deadline_policy, expected_utc, notify_utc)
        return int(wid)

    def list_active(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM watchers WHERE status IN ('active','breached') "
            "ORDER BY notify_at LIMIT ?",
            (limit,),
        ).fetchall()

    def cancel(self, watcher_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE watchers SET status = 'cancelled', updated_at = datetime('now') "
            "WHERE id = ? AND status IN ('active','breached')",
            (watcher_id,),
        )
        self.conn.commit()
        self._remove_jobs(watcher_id)
        return cur.rowcount > 0

    async def probe(self, watcher_id: int) -> str:
        """Return resolved, late, missing, or inactive after a live source read."""
        row = self.conn.execute(
            "SELECT * FROM watchers WHERE id = ?", (watcher_id,)
        ).fetchone()
        if row is None:
            return "inactive"
        if row["status"] == "resolved":
            return "resolved"
        if row["status"] not in ("active", "breached"):
            return "inactive"
        verifier = self._verifiers.get(row["source"])
        if verifier is None:
            raise RuntimeError(f"no verifier registered for {row['source']}")
        result = verifier(json.loads(row["criteria"]))
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return "missing"
        return self._record_match(row, result)

    def observe(self, source: str, payload: dict) -> list[int]:
        """Resolve or record a late match before general notification rules run."""
        matched: list[int] = []
        for row in self.conn.execute(
            "SELECT * FROM watchers WHERE status IN ('active','breached') AND source = ?",
            (source.lower(),),
        ).fetchall():
            criteria = json.loads(row["criteria"])
            if source.lower() == "gmail" and self._matches_email(criteria, payload):
                state = self._record_match(row, payload)
                if state in ("resolved", "late"):
                    matched.append(int(row["id"]))
        return matched

    def rehydrate(self) -> int:
        # A process can die after claiming a delivery but before Telegram
        # acknowledges it.  Recover that transient state and retry immediately
        # instead of losing the commitment forever.
        self.conn.execute(
            "UPDATE watchers SET status = 'breached', updated_at = datetime('now') "
            "WHERE status = 'delivering'"
        )
        self.conn.commit()
        now = datetime.now(timezone.utc)
        count = 0
        for row in self.list_active(limit=10_000):
            wid = int(row["id"])
            expected_by = datetime.fromisoformat(row["expected_by"])
            notify_at = datetime.fromisoformat(row["notify_at"])
            if row["status"] == "active" and row["deadline_policy"] == "by_deadline":
                self._schedule_deadline(wid, max(expected_by, now))
            self._schedule_notify(wid, max(notify_at, now))
            count += 1
        log.info("rehydrated %d conditional watcher(s)", count)
        return count

    async def _evaluate_deadline(self, watcher_id: int) -> None:
        row = self.conn.execute(
            "SELECT deadline_policy FROM watchers WHERE id = ?", (watcher_id,)
        ).fetchone()
        if row is None or row["deadline_policy"] != "by_deadline":
            return
        try:
            state = await self.probe(watcher_id)
        except Exception:
            log.exception("watcher %d deadline verification failed", watcher_id)
            self._schedule_deadline(
                watcher_id, datetime.now(timezone.utc) + RETRY_DELAY
            )
            return
        if state in ("resolved", "late", "inactive"):
            return
        self.conn.execute(
            """UPDATE watchers
               SET status = 'breached', breached_at = datetime('now'),
                   updated_at = datetime('now')
               WHERE id = ? AND status = 'active'""",
            (watcher_id,),
        )
        self.conn.commit()

    async def _evaluate_notify(self, watcher_id: int) -> None:
        try:
            state = await self.probe(watcher_id)
        except Exception:
            log.exception("watcher %d notification verification failed", watcher_id)
            row = self.conn.execute(
                "SELECT * FROM watchers WHERE id = ?", (watcher_id,)
            ).fetchone()
            already_notified = self._verification_error_notified(watcher_id)
            try:
                if not already_notified and row is not None and self.deliver:
                    await self.deliver(
                        watcher_id,
                        f"I couldn't verify “{row['description']}” because Gmail "
                        "isn't reachable. I'm keeping the watch open and will retry.",
                    )
                    # Record the notification only after Telegram accepted it.
                    # A failed attempt remains eligible on the retry.
                    self._mark_verification_error(watcher_id)
            except Exception:
                log.exception(
                    "watcher %d verification-error delivery failed; retrying",
                    watcher_id,
                )
            finally:
                self._schedule_notify(
                    watcher_id, datetime.now(timezone.utc) + RETRY_DELAY
                )
            return
        if state in ("resolved", "inactive"):
            return

        row = self.conn.execute(
            "SELECT * FROM watchers WHERE id = ?", (watcher_id,)
        ).fetchone()
        if row is None or row["status"] not in ("active", "breached"):
            return
        text = row["late_text"] if state == "late" and row["late_text"] else row["fallback_text"]
        previous_status = row["status"]
        claimed = self.conn.execute(
            "UPDATE watchers SET status = 'delivering', updated_at = datetime('now') "
            "WHERE id = ? AND status IN ('active','breached')",
            (watcher_id,),
        )
        self.conn.commit()
        if not claimed.rowcount:
            return
        if self.deliver:
            try:
                await self.deliver(watcher_id, text)
            except Exception:
                log.exception("watcher %d Telegram delivery failed; retrying", watcher_id)
                self.conn.execute(
                    "UPDATE watchers SET status = ?, updated_at = datetime('now') "
                    "WHERE id = ? AND status = 'delivering'",
                    (previous_status, watcher_id),
                )
                self.conn.commit()
                self._schedule_notify(
                    watcher_id, datetime.now(timezone.utc) + RETRY_DELAY
                )
                return
        self.conn.execute(
            "UPDATE watchers SET status = 'fired', updated_at = datetime('now') "
            "WHERE id = ? AND status = 'delivering'",
            (watcher_id,),
        )
        self.conn.commit()
        self._remove_jobs(watcher_id)

    def _record_match(self, row: sqlite3.Row, evidence: dict) -> str:
        arrived = self._arrival_time(evidence)
        expected = datetime.fromisoformat(row["expected_by"])
        on_time = arrived is not None and arrived <= expected
        if row["deadline_policy"] == "missing_at_notify" or on_time:
            cur = self.conn.execute(
                """UPDATE watchers
                   SET status = 'resolved', resolved_at = datetime('now'),
                       resolution_payload = ?, updated_at = datetime('now')
                   WHERE id = ? AND status IN ('active','breached')""",
                (json.dumps(evidence, ensure_ascii=False, default=str), row["id"]),
            )
            self.conn.commit()
            if cur.rowcount:
                self._remove_jobs(int(row["id"]))
            return "resolved"

        # It arrived, but after the promised deadline.  Preserve the breach so
        # the owner gets a useful "late, but here now" update at notify_at.
        self.conn.execute(
            """UPDATE watchers
               SET status = 'breached', breached_at = COALESCE(breached_at, datetime('now')),
                   resolution_payload = ?, updated_at = datetime('now')
               WHERE id = ? AND status IN ('active','breached')""",
            (json.dumps(evidence, ensure_ascii=False, default=str), row["id"]),
        )
        self.conn.commit()
        self._remove_job(self._deadline_job_id(int(row["id"])))
        return "late"

    def _verification_error_notified(self, watcher_id: int) -> bool:
        row = self.conn.execute(
            "SELECT resolution_payload FROM watchers WHERE id = ?", (watcher_id,)
        ).fetchone()
        payload: dict = {}
        if row and row["resolution_payload"]:
            try:
                payload = json.loads(row["resolution_payload"])
            except json.JSONDecodeError:
                payload = {}
        return bool(payload.get("verification_error_notified"))

    def _mark_verification_error(self, watcher_id: int) -> None:
        row = self.conn.execute(
            "SELECT resolution_payload FROM watchers WHERE id = ?", (watcher_id,)
        ).fetchone()
        payload: dict = {}
        if row and row["resolution_payload"]:
            try:
                payload = json.loads(row["resolution_payload"])
            except json.JSONDecodeError:
                payload = {}
        payload["verification_error_notified"] = True
        self.conn.execute(
            "UPDATE watchers SET resolution_payload = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(payload), watcher_id),
        )
        self.conn.commit()

    @staticmethod
    def _matches_email(criteria: dict, payload: dict) -> bool:
        sender = str(payload.get("from", "")).casefold()
        wanted_sender = str(criteria.get("sender_contains", "")).casefold()
        if wanted_sender and wanted_sender not in sender:
            return False
        subject = str(payload.get("subject", "")).casefold()
        wanted_subject = str(criteria.get("subject_contains", "")).casefold()
        if wanted_subject and wanted_subject not in subject:
            return False
        since = criteria.get("since")
        arrived = WatcherService._arrival_time(payload)
        if since and (arrived is None or arrived <= datetime.fromisoformat(str(since))):
            return False
        return True

    @staticmethod
    def _arrival_time(payload: dict) -> datetime | None:
        internal_ms = payload.get("internal_date")
        if internal_ms in (None, ""):
            return None
        try:
            return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    def _schedule_deadline(self, watcher_id: int, run_at: datetime) -> None:
        self.scheduler.add_job(
            self._evaluate_deadline,
            "date",
            run_date=run_at,
            args=[watcher_id],
            id=self._deadline_job_id(watcher_id),
            replace_existing=True,
            misfire_grace_time=24 * 60 * 60,
        )

    def _ensure_jobs(
        self,
        watcher_id: int,
        deadline_policy: str,
        expected_by: datetime,
        notify_at: datetime,
    ) -> None:
        if deadline_policy == "by_deadline":
            self._schedule_deadline(watcher_id, expected_by)
        self._schedule_notify(watcher_id, notify_at)

    def _schedule_notify(self, watcher_id: int, run_at: datetime) -> None:
        self.scheduler.add_job(
            self._evaluate_notify,
            "date",
            run_date=run_at,
            args=[watcher_id],
            id=self._notify_job_id(watcher_id),
            replace_existing=True,
            misfire_grace_time=24 * 60 * 60,
        )

    def _remove_jobs(self, watcher_id: int) -> None:
        self._remove_job(self._deadline_job_id(watcher_id))
        self._remove_job(self._notify_job_id(watcher_id))

    def _remove_job(self, job_id: str) -> None:
        job = self.scheduler.get_job(job_id)
        if job:
            job.remove()

    @staticmethod
    def _deadline_job_id(watcher_id: int) -> str:
        return f"watcher-{watcher_id}-deadline"

    @staticmethod
    def _notify_job_id(watcher_id: int) -> str:
        return f"watcher-{watcher_id}-notify"

    def _to_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.tz)
        return value.astimezone(timezone.utc)
