"""reminders_* tools (BRIEF §8)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..reminders import ReminderService
from .registry import Tool, ToolRegistry


def _human_due(
    due: datetime,
    owner_tz: ZoneInfo,
    *,
    now: datetime | None = None,
) -> str:
    # Tool input may be the documented naive local ISO form. Never compare it
    # with the VM's UTC wall clock or display an aware UTC input as owner time.
    local_due = (
        due.replace(tzinfo=owner_tz)
        if due.tzinfo is None
        else due.astimezone(owner_tz)
    )
    current = now or datetime.now(owner_tz)
    current = (
        current.replace(tzinfo=owner_tz)
        if current.tzinfo is None
        else current.astimezone(owner_tz)
    )
    clock = local_due.strftime("%I:%M %p").lstrip("0").casefold()
    day_offset = (local_due.date() - current.date()).days
    if day_offset == 0:
        return f"today at {clock}"
    if day_offset == 1:
        return f"tomorrow at {clock}"
    return f"on {local_due:%a} {local_due.day} {local_due:%b} at {clock}"


def register(registry: ToolRegistry, reminders: ReminderService) -> None:
    def reminders_set(text: str, due_at: str, recurrence: str | None = None) -> str:
        due = datetime.fromisoformat(due_at)
        rid = reminders.set(text, due, recurrence)
        when = _human_due(due, reminders.tz)
        if recurrence:
            when += ", then on the recurring schedule"
        # This receipt is also the reliable owner-facing fallback when the model
        # emits empty/canned prose after the mutation. Keep both what and when.
        return f"I'll remind you {when} — {text}"

    def reminders_cancel(reminder_id: int) -> str:
        ok = reminders.cancel(int(reminder_id))
        return f"Reminder #{reminder_id} cancelled." if ok else f"No active reminder #{reminder_id}."

    def reminders_list() -> str:
        rows = reminders.list_upcoming()
        if not rows:
            return "No upcoming reminders."
        return "\n".join(
            f"#{r['id']} {r['due_at']}" + (f" (recurring {r['recurrence']})" if r["recurrence"] else "")
            + f": {r['text']}"
            for r in rows
        )

    registry.register(Tool(
        name="reminders_set",
        description=(
            "Schedule a reminder. Call this whenever the owner asks to be "
            "reminded of anything. Compose the `text` as the exact message "
            "they will receive at fire time (it is delivered verbatim, "
            "without you). `due_at` is ISO-8601 local time, e.g. "
            "'2026-07-22T18:00'. For repeating reminders pass `recurrence` "
            "as a 5-field APScheduler cron expression (weekday 0 is Monday; "
            "e.g. '0 9 * * 0' = Mondays 9am) and set due_at to the earliest "
            "date the recurrence may begin. If the owner ties a reminder to a "
            "meeting but omits its time, resolve it from recent context or call "
            "calendar_list_events before asking. Common short/full first-name "
            "forms such as Phil/Philip/Phillip may be treated as the same person "
            "only when exactly one upcoming meeting matches. For an unspecified "
            "'before', use 10 minutes before and confirm both what and when; never "
            "finish with only 'Done'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The message delivered at fire time."},
                "due_at": {"type": "string", "description": "ISO-8601 datetime, owner's local timezone."},
                "recurrence": {"type": "string", "description": "Optional 5-field cron expression for repeats."},
            },
            "required": ["text", "due_at"],
        },
        func=reminders_set,
    ))

    registry.register(Tool(
        name="reminders_cancel",
        description="Cancel an upcoming reminder by id. Use reminders_list first if you don't know the id.",
        input_schema={
            "type": "object",
            "properties": {"reminder_id": {"type": "integer"}},
            "required": ["reminder_id"],
        },
        func=reminders_cancel,
    ))

    registry.register(Tool(
        name="reminders_list",
        description="List upcoming reminders. Call before cancelling or when the owner asks what's scheduled.",
        input_schema={"type": "object", "properties": {}},
        func=reminders_list,
    ))
