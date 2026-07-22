"""Conditional watcher tools: follow through without asking twice."""

from __future__ import annotations

from datetime import datetime

from ..watchers import WatcherService
from .registry import Tool, ToolRegistry


def register(registry: ToolRegistry, watchers: WatcherService) -> None:
    async def watchers_set_email(
        description: str,
        sender_contains: str,
        expected_by: str,
        notify_at: str,
        fallback_text: str,
        subject_contains: str | None = None,
        since: str | None = None,
        late_text: str | None = None,
        deadline_policy: str = "by_deadline",
    ) -> str:
        wid = watchers.set_email(
            description=description,
            sender_contains=sender_contains,
            subject_contains=subject_contains,
            expected_by=datetime.fromisoformat(expected_by),
            notify_at=datetime.fromisoformat(notify_at),
            fallback_text=fallback_text,
            since=datetime.fromisoformat(since) if since else None,
            late_text=late_text or (
                f"{sender_contains}'s email arrived after the deadline, so no "
                "check-in is needed now."
            ),
            deadline_policy=deadline_policy,
        )
        try:
            state = await watchers.probe(wid)
        except Exception:
            # The persisted watch is still valid and will reconcile at notify_at;
            # do not make the owner repeat the request because an immediate read
            # happened to fail.
            state = "verification_error"
        if state == "resolved":
            return (
                f"I checked first: the matching email from {sender_contains} is "
                "already there, so no fallback will fire."
            )
        if state == "late":
            return (
                f"I checked first: {sender_contains}'s email is there, but it "
                "arrived after the deadline. I'll flag that at the requested time."
            )
        if state == "verification_error":
            return (
                "I saved the watch, but Gmail wasn't reachable for the first "
                "check. I'll retry automatically and won't claim the email is "
                "missing unless I can verify it."
            )
        return (
            f"Checked—nothing matching from {sender_contains} yet. I'm watching "
            f"for it; if it is still missing, I'll notify you at {notify_at}."
        )

    def watchers_list() -> str:
        rows = watchers.list_active()
        if not rows:
            return "No active conditional watches."
        return "\n".join(
            f"#{r['id']} {r['description']} — expected {r['expected_by']}; "
            f"fallback {r['notify_at']}"
            for r in rows
        )

    def watchers_cancel(watcher_id: int) -> str:
        if watchers.cancel(int(watcher_id)):
            return f"Conditional watch #{watcher_id} cancelled."
        return f"No active conditional watch #{watcher_id}."

    registry.register(Tool(
        name="watchers_set_email",
        description=(
            "Create a conditional Gmail watch. Use this INSTEAD of a static "
            "reminder whenever the owner says 'if an email has not arrived', "
            "'watch for an email', 'let me know if they do not send it', or "
            "similar. This is a private reversible action: check Gmail and set "
            "the watch immediately in the same turn; NEVER ask permission to "
            "search first and NEVER ask again before setting it. The watch "
            "automatically resolves if a matching email arrives and verifies "
            "Gmail live at notify_at before sending fallback_text. Datetimes are "
            "ISO-8601 in the owner's local timezone. If `since` is omitted, "
            "the watch checks from the start of the owner's current local day; "
            "only provide it when the owner names a different starting point."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "sender_contains": {
                    "type": "string",
                    "description": "Sender name or address fragment, e.g. Shyan or shyan@example.com.",
                },
                "subject_contains": {
                    "type": "string",
                    "description": "Optional subject fragment when sender alone is too broad.",
                },
                "expected_by": {
                    "type": "string",
                    "description": "When the email was promised/due, ISO-8601 local datetime.",
                },
                "notify_at": {
                    "type": "string",
                    "description": "When to alert if still missing, ISO-8601 local datetime.",
                },
                "fallback_text": {
                    "type": "string",
                    "description": "Exact concise message to push if the email is still missing.",
                },
                "since": {
                    "type": "string",
                    "description": "Optional ISO-8601 lower bound for matching arrivals.",
                },
                "late_text": {
                    "type": "string",
                    "description": "Optional message if the email arrives after expected_by but before notify_at.",
                },
                "deadline_policy": {
                    "type": "string",
                    "enum": ["by_deadline", "missing_at_notify"],
                    "description": "Use by_deadline when lateness itself matters; missing_at_notify when any arrival before the notification resolves it.",
                },
            },
            "required": [
                "description",
                "sender_contains",
                "expected_by",
                "notify_at",
                "fallback_text",
            ],
        },
        func=watchers_set_email,
    ))

    registry.register(Tool(
        name="watchers_list",
        description=(
            "List active conditional watches. Use when the owner asks what you "
            "are watching or waiting on."
        ),
        input_schema={"type": "object", "properties": {}},
        func=watchers_list,
    ))

    registry.register(Tool(
        name="watchers_cancel",
        description=(
            "Cancel an active conditional watch by id. Use watchers_list first "
            "when the id is unknown."
        ),
        input_schema={
            "type": "object",
            "properties": {"watcher_id": {"type": "integer"}},
            "required": ["watcher_id"],
        },
        func=watchers_cancel,
    ))
