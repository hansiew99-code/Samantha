"""calendar_* tools (BRIEF §8). Blocking Google calls run via asyncio.to_thread."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..integrations.gcal import GCalClient, find_free_slots
from .registry import Tool, ToolRegistry


def register(registry: ToolRegistry, gcal: GCalClient, tz: str, owner_email: str = "primary") -> None:
    zone = ZoneInfo(tz)

    async def calendar_list_events(start: str, end: str) -> str:
        events = await asyncio.to_thread(
            gcal.list_events, _iso(start, zone), _iso(end, zone)
        )
        if not events:
            return "No events in that window."
        return "\n".join(
            f"[{e['id']}] {e['start']} → {e['end']}: {e['summary']}"
            + (f" @ {e['location']}" if e["location"] else "")
            + (f" (with {', '.join(e['attendees'])})" if e["attendees"] else "")
            for e in events
        )

    async def calendar_create_event(
        summary: str, start: str, end: str,
        description: str | None = None, location: str | None = None,
        attendees: list[str] | None = None,
    ) -> str:
        ev = await asyncio.to_thread(
            gcal.create_event, summary, _iso(start, zone), _iso(end, zone),
            description, location, attendees,
        )
        return f"Created event {ev['id']}: {summary} ({start} → {end})."

    async def calendar_update_event(
        event_id: str, summary: str | None = None, start: str | None = None,
        end: str | None = None, description: str | None = None, location: str | None = None,
    ) -> str:
        patch: dict = {}
        if summary is not None:
            patch["summary"] = summary
        if start is not None:
            patch["start_iso"] = _iso(start, zone)
        if end is not None:
            patch["end_iso"] = _iso(end, zone)
        if description is not None:
            patch["description"] = description
        if location is not None:
            patch["location"] = location
        ev = await asyncio.to_thread(gcal.update_event, event_id, **patch)
        return f"Updated event {ev['id']}."

    async def calendar_delete_event(event_id: str) -> str:
        await asyncio.to_thread(gcal.delete_event, event_id)
        return f"Deleted event {event_id}."

    async def calendar_find_slots(
        attendees: list[str], duration_minutes: int, window_start: str, window_end: str,
    ) -> str:
        start_dt = datetime.fromisoformat(window_start).replace(tzinfo=zone) \
            if datetime.fromisoformat(window_start).tzinfo is None \
            else datetime.fromisoformat(window_start)
        end_dt = datetime.fromisoformat(window_end).replace(tzinfo=zone) \
            if datetime.fromisoformat(window_end).tzinfo is None \
            else datetime.fromisoformat(window_end)
        emails = list(dict.fromkeys([*attendees, owner_email]))
        busy = await asyncio.to_thread(
            gcal.freebusy, emails, start_dt.isoformat(), end_dt.isoformat()
        )
        slots = find_free_slots(busy, start_dt, end_dt, timedelta(minutes=duration_minutes))
        if not slots:
            return "No mutual free slots in that window (within working hours 09:00-18:00)."
        return "Mutual free slots:\n" + "\n".join(
            f"- {s.strftime('%a %d %b %H:%M')}–{e.strftime('%H:%M')}" for s, e in slots
        )

    registry.register(Tool(
        name="calendar_list_events",
        description=(
            "List the owner's calendar events between two datetimes. Call this "
            "before answering any question about their schedule — never guess."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "ISO-8601 datetime (owner's timezone)."},
                "end": {"type": "string", "description": "ISO-8601 datetime (owner's timezone)."},
            },
            "required": ["start", "end"],
        },
        func=calendar_list_events,
    ))

    registry.register(Tool(
        name="calendar_create_event",
        description=(
            "Create an event on the owner's calendar. The owner's own calendar "
            "is yours to manage — no approval needed. Datetimes are ISO-8601 "
            "in the owner's timezone."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee emails (they receive invites)."},
            },
            "required": ["summary", "start", "end"],
        },
        func=calendar_create_event,
    ))

    registry.register(Tool(
        name="calendar_update_event",
        description="Reschedule or edit an existing event by id (get ids from calendar_list_events).",
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "summary": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["event_id"],
        },
        func=calendar_update_event,
    ))

    registry.register(Tool(
        name="calendar_delete_event",
        description="Delete an event from the owner's calendar by id.",
        input_schema={
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
        func=calendar_delete_event,
    ))

    registry.register(Tool(
        name="calendar_find_slots",
        description=(
            "Find mutual free time slots across the owner and other people's "
            "Google calendars (their free/busy must be visible to the owner). "
            "Call this whenever the owner asks when they and someone else can "
            "meet. The intersection is computed exactly — present the returned "
            "slots, don't invent alternatives."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Other attendees' emails."},
                "duration_minutes": {"type": "integer"},
                "window_start": {"type": "string", "description": "ISO-8601 start of the search window."},
                "window_end": {"type": "string", "description": "ISO-8601 end of the search window."},
            },
            "required": ["attendees", "duration_minutes", "window_start", "window_end"],
        },
        func=calendar_find_slots,
    ))


def _iso(value: str, zone: ZoneInfo) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.isoformat()
