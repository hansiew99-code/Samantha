"""calendar_* tools (BRIEF §8). Blocking Google calls run via asyncio.to_thread."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from ..actions import PendingActions
from ..integrations.gcal import GCalClient, find_free_slots
from .registry import Tool, ToolRegistry

NotifyDraft = Callable[[int, str], Awaitable[None]]


def register(
    registry: ToolRegistry,
    gcal: GCalClient,
    tz: str,
    actions: PendingActions,
    notify_draft: NotifyDraft,
    owner_email: str = "primary",
) -> None:
    zone = ZoneInfo(tz)

    async def calendar_list_events(start: str, end: str) -> str:
        events = await asyncio.to_thread(
            gcal.list_events, _iso(start, zone), _iso(end, zone), 21
        )
        if not events:
            return "No events in that window."
        listing = "\n".join(
            f"[{e['id']}] {e['start']} → {e['end']}: {e['summary']}"
            + (f" @ {e['location']}" if e["location"] else "")
            + (f" (with {', '.join(e['attendees'])})" if e["attendees"] else "")
            for e in events[:20]
        )
        if len(events) > 20:
            listing += "\n[More than 20 events match; narrow the date window for the rest.]"
        return listing

    async def calendar_create_event(
        summary: str, start: str, end: str,
        description: str | None = None, location: str | None = None,
        attendees: list[str] | None = None,
    ) -> str:
        if attendees:
            preview_lines = [
                f"📅 Invite: {summary}",
                f"{start} → {end}",
                f"Guests: {', '.join(attendees)}",
            ]
            if location:
                preview_lines.append(f"Location: {location}")
            if description:
                preview_lines.append(f"Description: {description}")
            preview = "\n".join(preview_lines)
            action_id = actions.create(
                "calendar_create_with_attendees",
                {
                    "summary": summary,
                    "start": _iso(start, zone),
                    "end": _iso(end, zone),
                    "description": description,
                    "location": location,
                    "attendees": attendees,
                },
                preview,
            )
            await notify_draft(action_id, preview)
            return (
                f"Calendar invite draft #{action_id} is ready for approval. "
                "It has not been created or sent yet."
            )
        ev = await asyncio.to_thread(
            gcal.create_event, summary, _iso(start, zone), _iso(end, zone),
            description, location, attendees,
        )
        if ev.get("deduplicated"):
            return (
                f"That event already exists as {ev['id']}: {summary} "
                f"({start} → {end}); I did not create a duplicate."
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
        if not patch:
            return "No calendar changes were requested."
        current = await asyncio.to_thread(gcal.get_event, event_id)
        attendees = current.get("attendees", [])
        if attendees:
            changes = ", ".join(f"{key}={value}" for key, value in patch.items())
            preview = (
                f"📅 Update: {current.get('summary', event_id)}\n"
                f"Changes: {changes}\nGuests: {', '.join(attendees)}"
            )
            action_id = actions.create(
                "calendar_update_with_attendees",
                {"event_id": event_id, "patch": patch},
                preview,
            )
            await notify_draft(action_id, preview)
            return (
                f"Calendar update #{action_id} is ready for approval. "
                "The shared event has not been changed and guests have not been notified."
            )
        ev = await asyncio.to_thread(
            gcal.update_event, event_id, send_updates=False, **patch
        )
        return f"Updated event {ev['id']}."

    async def calendar_delete_event(event_id: str) -> str:
        current = await asyncio.to_thread(gcal.get_event, event_id)
        preview_lines = [
            f"🗑 Delete calendar event: {current.get('summary', event_id)}",
            f"When: {current.get('start') or '?'} → {current.get('end') or '?'}",
        ]
        if current.get("location"):
            preview_lines.append(f"Location: {current['location']}")
        if current.get("attendees"):
            preview_lines.append(f"Guests notified: {', '.join(current['attendees'])}")
        preview_lines.append(f"Event id: {event_id}")
        preview = "\n".join(preview_lines)
        action_id = actions.create(
            "calendar_delete", {"event_id": event_id}, preview
        )
        await notify_draft(action_id, preview)
        return (
            f"Calendar deletion #{action_id} is waiting for approval. "
            "Nothing has been deleted yet."
        )

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
            "is yours to manage with no approval when there are no attendees. "
            "If attendees are present, this automatically creates an approval "
            "draft because accepting it sends invitations to other people. "
            "Datetimes are ISO-8601 in the owner's timezone."
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
        description=(
            "Reschedule or edit an existing event by id (get ids from "
            "calendar_list_events). Private events are changed immediately; "
            "shared events with attendees automatically become a Telegram "
            "approval draft before guests are affected."
        ),
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
        description=(
            "Prepare deletion of a calendar event by id. Deletion is destructive "
            "and may notify attendees, so this always goes through one-tap approval."
        ),
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
