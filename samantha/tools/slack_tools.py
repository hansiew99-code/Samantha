"""slack_* tools (BRIEF §8). Reading is local (captured events); sending goes
through the approval gate like all outbound.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Awaitable, Callable

from ..actions import PendingActions
from .registry import Tool, ToolRegistry

NotifyDraft = Callable[[int, str], Awaitable[None]]


def register(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    actions: PendingActions,
    notify_draft: NotifyDraft,
) -> None:
    def slack_recent(hours: int = 24) -> str:
        rows = conn.execute(
            "SELECT kind, scope, payload, created_at FROM events_queue "
            "WHERE source = 'slack' AND created_at >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT 20",
            (f"-{int(hours)} hours",),
        ).fetchall()
        if not rows:
            return "Nothing from Slack in that window."
        lines = []
        for r in rows:
            p = json.loads(r["payload"])
            lines.append(
                f"[{r['created_at']}] {r['kind']} in {p.get('channel')} "
                f"from {p.get('user')}: {p.get('text', '')}"
            )
        return "\n".join(lines)

    async def slack_draft_reply(channel: str, text: str) -> str:
        preview = f"💬 Slack → {channel}:\n\n{text}"
        action_id = actions.create(
            "slack_send", {"channel": channel, "text": text}, preview
        )
        await notify_draft(action_id, preview)
        return (
            f"Draft #{action_id} sent to the owner for approval. It is NOT "
            "posted yet — do not draft it again."
        )

    registry.register(Tool(
        name="slack_recent",
        description=(
            "List recent Slack mentions and DMs captured for the owner. Call "
            "this before answering anything about Slack activity."
        ),
        input_schema={
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Lookback window, default 24."}},
        },
        func=slack_recent,
    ))

    registry.register(Tool(
        name="slack_draft_reply",
        description=(
            "Draft a Slack message for the owner's approval. NEVER posts "
            "directly — the owner gets a Send button in Telegram. `channel` "
            "is the channel/DM id from slack_recent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["channel", "text"],
        },
        func=slack_draft_reply,
    ))
