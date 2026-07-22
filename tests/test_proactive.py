"""Proactive scanner: pure calendar detectors + the scan loop (push once,
dedup, suppress rules, quiet hours)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from samantha.proactive import (
    ProactiveScanner,
    find_conflicts,
    imminent_meetings,
)
from samantha.rules import RulesEngine

TZ = ZoneInfo("Asia/Kuala_Lumpur")


def ev(eid: str, summary: str, start: datetime, end: datetime, location=None) -> dict:
    return {
        "id": eid,
        "summary": summary,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "location": location,
        "attendees": [],
    }


class FakeGCal:
    def __init__(self, events: list[dict]) -> None:
        self.events = events

    def list_events(self, start: str, end: str, max_results: int = 20) -> list[dict]:
        return self.events


# -- pure detectors -----------------------------------------------------------


def test_imminent_meetings_only_within_lead_and_not_started():
    now = datetime(2026, 7, 22, 9, 0, tzinfo=TZ)
    events = [
        ev("a", "soon", now + timedelta(minutes=10), now + timedelta(minutes=40)),
        ev("b", "later", now + timedelta(hours=3), now + timedelta(hours=4)),
        ev("c", "already started", now - timedelta(minutes=5), now + timedelta(minutes=25)),
        {"id": "d", "summary": "all-day", "start": "2026-07-22", "end": "2026-07-23", "attendees": []},
    ]
    got = imminent_meetings(events, now, TZ)
    assert [e["id"] for e, _ in got] == ["a"]


def test_find_conflicts_flags_overlap_not_touch():
    now = datetime(2026, 7, 22, 9, 0, tzinfo=TZ)
    a = ev("a", "A", now + timedelta(hours=1), now + timedelta(hours=2))
    b = ev("b", "B", now + timedelta(hours=1, minutes=30), now + timedelta(hours=1, minutes=45))
    c = ev("c", "C", now + timedelta(hours=2), now + timedelta(hours=3))  # touches A's end
    pairs = {tuple(sorted([x["id"], y["id"]])) for x, y in find_conflicts([a, b, c], now, TZ)}
    assert ("a", "b") in pairs
    assert ("a", "c") not in pairs  # back-to-back is not a conflict
    assert ("b", "c") not in pairs


# -- the scan loop ------------------------------------------------------------


def make_scanner(settings, conn, memory, events, notifications):
    rules = RulesEngine(conn, memory)

    async def notify(text: str) -> None:
        notifications.append(text)

    # Open the working window to all hours/days so the scan runs whenever the
    # suite runs. Off-hours gating gets its own test below.
    settings.work_days = "mon-sun"
    settings.work_start_hour = 0
    settings.work_end_hour = 24
    return ProactiveScanner(settings, conn, rules, notify, gcal=FakeGCal(events)), rules


async def test_scan_pushes_imminent_meeting_exactly_once(settings, conn, memory):
    now = datetime.now(TZ)
    events = [ev("m1", "Standup", now + timedelta(minutes=10), now + timedelta(minutes=25), location="Zoom")]
    notifications: list[str] = []
    scanner, _ = make_scanner(settings, conn, memory, events, notifications)

    assert await scanner.scan() == 1
    assert "Standup" in notifications[0] and "Zoom" in notifications[0]
    # Runs every 10 min — but the same meeting must never be pushed twice.
    assert await scanner.scan() == 0
    assert len(notifications) == 1


async def test_scan_obeys_a_calendar_suppress_rule(settings, conn, memory):
    now = datetime.now(TZ)
    events = [ev("m1", "Standup", now + timedelta(minutes=5), now + timedelta(minutes=20))]
    notifications: list[str] = []
    scanner, rules = make_scanner(settings, conn, memory, events, notifications)
    rules.add("calendar", "suppress")

    assert await scanner.scan() == 0
    assert notifications == []


async def test_scan_flags_a_double_booking(settings, conn, memory):
    now = datetime.now(TZ)
    a = ev("a", "Dentist", now + timedelta(hours=2), now + timedelta(hours=3))
    b = ev("b", "Client call", now + timedelta(hours=2, minutes=30), now + timedelta(hours=3, minutes=30))
    notifications: list[str] = []
    scanner, _ = make_scanner(settings, conn, memory, [a, b], notifications)

    assert await scanner.scan() == 1
    assert "double-booked" in notifications[0]
    assert await scanner.scan() == 0  # deduped


async def test_scan_noops_without_calendar(settings, conn, memory):
    notifications: list[str] = []
    rules = RulesEngine(conn, memory)

    async def notify(text: str) -> None:
        notifications.append(text)

    scanner = ProactiveScanner(settings, conn, rules, notify, gcal=None)
    assert await scanner.scan() == 0


async def test_scan_rests_outside_working_hours(settings, conn, memory):
    now = datetime.now(TZ)
    events = [ev("m1", "Standup", now + timedelta(minutes=10), now + timedelta(minutes=25))]
    notifications: list[str] = []
    scanner, _ = make_scanner(settings, conn, memory, events, notifications)
    # Constrain the window to a day that isn't today → she's off the clock.
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    settings.work_days = names[(now.weekday() + 1) % 7]

    assert await scanner.scan() == 0
    assert notifications == []
