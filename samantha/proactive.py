"""Unprompted proactivity: a scheduled scan that looks ahead on the owner's
calendar on its own and pushes what a sharp assistant would flag — a meeting
about to start, a double-booking — with nobody asking it to.

This is the piece that makes her initiate. The event sweep (events.py) reacts
to things that arrive (a new email, a Slack ping); this reacts to *time* and to
the *shape* of the calendar, which nothing else watches.

Deterministic and zero-token: the nudges are found and phrased in plain code,
so they cost nothing and run even when the daily budget is spent. Each distinct
nudge fires exactly once (dedup in integration_state), suppress rules apply
(source 'calendar'), and quiet hours are honoured — except an imminent meeting,
which is exactly the thing worth a late ping.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .db import kv_get, kv_set
from .events import in_quiet_hours
from .rules import RulesEngine

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]

MEETING_LEAD = timedelta(minutes=25)  # nudge once a meeting is within this window
CONFLICT_HORIZON = timedelta(hours=48)  # look this far ahead for double-bookings
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
    line = f"heads up — {ev.get('summary', 'your next thing')} {when}"
    if ev.get("location"):
        line += f", at {ev['location']}"
    return line


def conflict_line(a: dict, b: dict, tz: ZoneInfo) -> str:
    a_start = _parse_dt(a.get("start"), tz)
    day = a_start.strftime("%a") if a_start else "soon"
    at = _fmt_time(a_start) if a_start else ""
    return (
        f"you're double-booked {day} around {at}: “{a.get('summary', 'one')}” and "
        f"“{b.get('summary', 'another')}” overlap. want me to move one?"
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
        try:
            events = await asyncio.to_thread(
                self.gcal.list_events,
                now.isoformat(),
                (now + CONFLICT_HORIZON).isoformat(),
            )
        except Exception:
            log.exception("proactive scan: calendar fetch failed")
            return 0

        pushed = 0

        # Imminent meetings — worth a ping even inside quiet hours (a meeting
        # you'd otherwise miss is precisely why you'd want to be woken).
        for ev, start in imminent_meetings(events, now, tz):
            if not self.rules.allows(SOURCE, str(ev.get("summary", ""))):
                continue
            if self._fire_once(f"nudge:meeting:{ev.get('id')}", now):
                await self.notify(meeting_line(ev, start, now))
                pushed += 1

        # Conflicts — useful but not urgent; hold them until waking hours.
        if not in_quiet_hours(now, self.settings.quiet_hours):
            for a, b in find_conflicts(events, now, tz):
                ids = sorted([str(a.get("id")), str(b.get("id"))])
                if not self.rules.allows(SOURCE, str(a.get("summary", ""))):
                    continue
                if self._fire_once(f"nudge:conflict:{ids[0]}:{ids[1]}", now):
                    await self.notify(conflict_line(a, b, tz))
                    pushed += 1

        return pushed

    def _fire_once(self, key: str, now: datetime) -> bool:
        """True the first time a nudge key is seen; records it so it never
        fires again."""
        if kv_get(self.conn, key) is not None:
            return False
        kv_set(self.conn, key, now.isoformat())
        return True
