"""Unprompted proactivity: a scheduled scan that looks ahead on the owner's
calendar on its own and pushes what a sharp assistant would flag — a meeting
about to start, a double-booking — with nobody asking it to.

This is the piece that makes her initiate. The event sweep (events.py) reacts
to things that arrive (a new email, a Slack ping); this reacts to *time* and to
the *shape* of the calendar, which nothing else watches.

Deterministic and zero-token: the nudges are found and phrased in plain code,
so they cost nothing and run even when the daily budget is spent. Each distinct
nudge fires exactly once (dedup in integration_state), suppress rules apply
(source 'calendar'), and it only runs inside the working window (weekday
business hours) — off-hours she rests and the evening brief carries anything
pending.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .db import kv_get, kv_set
from .events import in_quiet_hours, in_work_hours
from .rules import RulesEngine

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]

MEETING_LEAD = timedelta(minutes=25)  # minimum; scan cadence can widen this
CONFLICT_HORIZON = timedelta(hours=48)  # look this far ahead for double-bookings
DEDUPE_TTL = timedelta(days=7)
SOURCE = "calendar"


# -- pure detectors (unit-tested, no API) -------------------------------------


def _parse_dt(value: str | None, tz: ZoneInfo) -> datetime | None:
    """Parse a calendar start/end. Returns None for all-day (date-only) events —
    those aren't meetings to nudge about."""
    if not value or "T" not in value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def imminent_meetings(
    events: list[dict], now: datetime, tz: ZoneInfo, lead: timedelta = MEETING_LEAD
) -> list[tuple[dict, datetime]]:
    """Timed events starting within [now, now+lead] that haven't begun yet."""
    out: list[tuple[dict, datetime]] = []
    for ev in events:
        start = _parse_dt(ev.get("start"), tz)
        if start is None:
            continue
        if now <= start <= now + lead:
            out.append((ev, start))
    out.sort(key=lambda pair: pair[1])
    return out


def find_conflicts(
    events: list[dict], now: datetime, tz: ZoneInfo, horizon: timedelta = CONFLICT_HORIZON
) -> list[tuple[dict, dict]]:
    """Pairs of timed events that overlap within the look-ahead horizon. Two
    events that merely touch (one ends as the next starts) don't count."""
    timed: list[tuple[dict, datetime, datetime]] = []
    for ev in events:
        start = _parse_dt(ev.get("start"), tz)
        end = _parse_dt(ev.get("end"), tz)
        if start is None or end is None:
            continue
        if end <= now or start > now + horizon:
            continue
        timed.append((ev, start, end))
    timed.sort(key=lambda t: t[1])
    conflicts: list[tuple[dict, dict]] = []
    for i in range(len(timed)):
        ev_a, a_start, a_end = timed[i]
        for j in range(i + 1, len(timed)):
            ev_b, b_start, b_end = timed[j]
            if b_start >= a_end:
                break  # sorted by start: nothing later can overlap A either
            if a_start < b_end and b_start < a_end and ev_a.get("id") != ev_b.get("id"):
                conflicts.append((ev_a, ev_b))
    return conflicts


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%-I:%M%p").lower() if hasattr(dt, "strftime") else str(dt)


def meeting_line(ev: dict, start: datetime, now: datetime) -> str:
    mins = max(1, round((start - now).total_seconds() / 60))
    when = f"in about {mins} min" if mins > 1 else "in a minute"
    line = f"{ev.get('summary', 'Your next thing')} starts {when}"
    if ev.get("location"):
        line += f" — {ev['location']}"
    return line + "."


def conflict_line(a: dict, b: dict, tz: ZoneInfo) -> str:
    a_start = _parse_dt(a.get("start"), tz)
    day = a_start.strftime("%a") if a_start else "soon"
    at = _fmt_time(a_start) if a_start else ""
    return (
        f"You're double-booked {day} around {at} — “{a.get('summary', 'one')}” and "
        f"“{b.get('summary', 'another')}” overlap, so which one should move?"
    )


# -- the scanner --------------------------------------------------------------


class ProactiveScanner:
    def __init__(
        self,
        settings: Settings,
        conn,
        rules: RulesEngine,
        notify: Notify,
        gcal=None,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.rules = rules
        self.notify = notify
        self.gcal = gcal

    async def scan(self) -> int:
        """Find and push new nudges. Returns how many were pushed."""
        if self.gcal is None:
            return 0
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.now(tz)
        if in_quiet_hours(now, self.settings.quiet_hours):
            return 0
        if not in_work_hours(
            now,
            self.settings.work_weekdays(),
            self.settings.work_start_hour,
            self.settings.work_end_hour,
        ):
            return 0  # off-hours she rests; the 21:00 brief carries anything pending
        try:
            events = await asyncio.to_thread(
                self.gcal.list_events,
                now.isoformat(),
                (now + CONFLICT_HORIZON).isoformat(),
            )
        except Exception:
            log.exception("proactive scan: calendar fetch failed")
            return 0

        # key, rendered thought, and any lower-value nudge keys that thought
        # already covers.
        candidates: list[tuple[str, str, tuple[str, ...]]] = []
        lead = max(
            MEETING_LEAD,
            timedelta(minutes=self.settings.proactive_interval_minutes + 5),
        )

        # Work out the actionable conflicts first. An event that is already
        # named in a double-booking should not also appear as an imminent-event
        # nudge in the same (or following) scan; the conflict carries the more
        # useful fact and names both meetings.
        conflicts: list[tuple[str, dict, dict]] = []
        conflicts_by_occurrence: dict[str, list[str]] = {}
        covered_meeting_keys: dict[str, list[str]] = {}
        for a, b in find_conflicts(events, now, tz):
            if not self.rules.allows(SOURCE, str(a.get("summary", ""))):
                continue
            if not self.rules.allows(SOURCE, str(b.get("summary", ""))):
                continue
            occurrences = sorted([
                self._occurrence_key(a, tz),
                self._occurrence_key(b, tz),
            ])
            key = f"nudge:conflict:{occurrences[0]}:{occurrences[1]}"
            if not self._unseen(key, now):
                continue
            conflicts.append((key, a, b))
            covered_meeting_keys[key] = []
            for occurrence in occurrences:
                conflicts_by_occurrence.setdefault(occurrence, []).append(key)

        # Meetings about to start.
        for ev, start in imminent_meetings(events, now, tz, lead=lead):
            if not self.rules.allows(SOURCE, str(ev.get("summary", ""))):
                continue
            # Include the occurrence time: providers can reuse an event id for
            # a recurring meeting, and a moved meeting deserves a fresh nudge.
            key = f"nudge:meeting:{ev.get('id')}:{start.isoformat()}"
            covering_conflicts = conflicts_by_occurrence.get(
                self._occurrence_key(ev, tz), []
            )
            if covering_conflicts:
                for conflict_key in covering_conflicts:
                    covered_meeting_keys[conflict_key].append(key)
                continue
            if self._unseen(key, now):
                candidates.append((key, meeting_line(ev, start, now), ()))

        # Double-bookings in the look-ahead window.
        for key, a, b in conflicts:
            candidates.append((
                key,
                conflict_line(a, b, tz),
                tuple(covered_meeting_keys[key]),
            ))

        if not candidates:
            return 0
        # One considered interruption per scan, with no more than two things
        # competing for attention. Separate thoughts read like a quick human
        # note rather than a numbered status report.
        bounded = candidates[:2]
        text = "\n\n".join(line for _key, line, _covered in bounded)
        try:
            await self.notify(text)
        except Exception:
            log.exception("proactive calendar notification failed")
            return 0
        for key, _line, covered in bounded:
            kv_set(self.conn, key, now.isoformat())
            for covered_key in covered:
                kv_set(self.conn, covered_key, now.isoformat())
        return len(bounded)

    def _unseen(self, key: str, now: datetime) -> bool:
        seen_at = kv_get(self.conn, key)
        return seen_at is None or self._dedupe_expired(seen_at, now)

    @staticmethod
    def _occurrence_key(event: dict, tz: ZoneInfo) -> str:
        start = _parse_dt(event.get("start"), tz)
        end = _parse_dt(event.get("end"), tz)
        return ":".join([
            str(event.get("id", "unknown")),
            start.isoformat() if start else str(event.get("start", "")),
            end.isoformat() if end else str(event.get("end", "")),
        ])

    @staticmethod
    def _dedupe_expired(value: str, now: datetime) -> bool:
        try:
            seen_at = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            # Preserve old/unknown dedupe entries rather than risk a burst of
            # repeated notifications after an upgrade.
            return False
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=now.tzinfo)
        return now - seen_at.astimezone(now.tzinfo) > DEDUPE_TTL
