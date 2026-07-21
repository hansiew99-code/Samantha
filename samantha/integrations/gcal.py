"""Google Calendar: thin API client + pure free/busy slot math.

The LLM never computes slots — `find_free_slots` is plain code (BRIEF §8);
the model only phrases the result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

Interval = tuple[datetime, datetime]

WORKING_HOURS = (time(9, 0), time(18, 0))


# -- pure slot math (unit-tested, zero tokens) --------------------------------


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def find_free_slots(
    busy: list[Interval],
    window_start: datetime,
    window_end: datetime,
    duration: timedelta,
    working_hours: tuple[time, time] = WORKING_HOURS,
    max_results: int = 5,
) -> list[Interval]:
    """Slots of `duration` inside [window_start, window_end], within working
    hours, avoiding every busy interval (all attendees' calendars combined)."""
    merged = merge_intervals(busy)
    slots: list[Interval] = []
    day = window_start.date()
    tz = window_start.tzinfo
    while day <= window_end.date() and len(slots) < max_results:
        day_start = max(window_start, datetime.combine(day, working_hours[0], tzinfo=tz))
        day_end = min(window_end, datetime.combine(day, working_hours[1], tzinfo=tz))
        cursor = day_start
        for b_start, b_end in merged:
            if b_end <= cursor or b_start >= day_end:
                continue
            if b_start - cursor >= duration:
                slots.append((cursor, cursor + duration))
                if len(slots) >= max_results:
                    return slots
            cursor = max(cursor, b_end)
        if day_end - cursor >= duration and len(slots) < max_results:
            slots.append((cursor, cursor + duration))
        day += timedelta(days=1)
    return slots


# -- API client (blocking; call via asyncio.to_thread) ------------------------


@dataclass
class GCalClient:
    creds: object
    tz: str
    calendar_id: str = "primary"

    def _service(self):
        from googleapiclient.discovery import build

        return build("calendar", "v3", credentials=self.creds, cache_discovery=False)

    def list_events(self, start_iso: str, end_iso: str, max_results: int = 20) -> list[dict]:
        resp = (
            self._service()
            .events()
            .list(
                calendarId=self.calendar_id,
                timeMin=start_iso,
                timeMax=end_iso,
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
        out = []
        for ev in resp.get("items", []):
            out.append(
                {
                    "id": ev.get("id"),
                    "summary": ev.get("summary", "(no title)"),
                    "start": ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date"),
                    "end": ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date"),
                    "location": ev.get("location"),
                    "attendees": [a.get("email") for a in ev.get("attendees", [])],
                }
            )
        return out

    def create_event(
        self,
        summary: str,
        start_iso: str,
        end_iso: str,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict:
        body: dict = {
            "summary": summary,
            "start": {"dateTime": start_iso, "timeZone": self.tz},
            "end": {"dateTime": end_iso, "timeZone": self.tz},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]
        ev = self._service().events().insert(calendarId=self.calendar_id, body=body).execute()
        return {"id": ev.get("id"), "link": ev.get("htmlLink")}

    def update_event(self, event_id: str, **patch: object) -> dict:
        body: dict = {}
        if "summary" in patch:
            body["summary"] = patch["summary"]
        if "start_iso" in patch:
            body["start"] = {"dateTime": patch["start_iso"], "timeZone": self.tz}
        if "end_iso" in patch:
            body["end"] = {"dateTime": patch["end_iso"], "timeZone": self.tz}
        if "description" in patch:
            body["description"] = patch["description"]
        if "location" in patch:
            body["location"] = patch["location"]
        ev = (
            self._service()
            .events()
            .patch(calendarId=self.calendar_id, eventId=event_id, body=body)
            .execute()
        )
        return {"id": ev.get("id"), "summary": ev.get("summary")}

    def delete_event(self, event_id: str) -> None:
        self._service().events().delete(calendarId=self.calendar_id, eventId=event_id).execute()

    def freebusy(self, emails: list[str], start_iso: str, end_iso: str) -> list[Interval]:
        """Busy intervals for every calendar in `emails` (needs only free/busy
        visibility, not full read access to other people's calendars)."""
        resp = (
            self._service()
            .freebusy()
            .query(
                body={
                    "timeMin": start_iso,
                    "timeMax": end_iso,
                    "items": [{"id": e} for e in emails],
                }
            )
            .execute()
        )
        tzinfo = ZoneInfo(self.tz)
        busy: list[Interval] = []
        for cal in resp.get("calendars", {}).values():
            for period in cal.get("busy", []):
                busy.append(
                    (
                        datetime.fromisoformat(period["start"].replace("Z", "+00:00")).astimezone(tzinfo),
                        datetime.fromisoformat(period["end"].replace("Z", "+00:00")).astimezone(tzinfo),
                    )
                )
        return busy
