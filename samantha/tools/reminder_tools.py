"""reminders_* tools (BRIEF §8)."""

from __future__ import annotations

from datetime import datetime

from ..reminders import ReminderService
from .registry import Tool, ToolRegistry


def register(registry: ToolRegistry, reminders: ReminderService) -> None:
    def reminders_set(text: str, due_at: str, recurrence: str | None = None) -> str:
        due = datetime.fromisoformat(due_at)
        rid = reminders.set(text, due, recurrence)
        when = "recurring " + recurrence if recurrence else f"once at {due_at}"
        return f"Reminder #{rid} set ({when}): {text}"

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
            "date the recurrence may begin."
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
