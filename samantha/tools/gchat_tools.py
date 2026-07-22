"""gchat_* tools (read-only). Google Chat has no send path here — the brain can
look at what people have said, but replying to Chat isn't wired (v1)."""

from __future__ import annotations

import asyncio

from ..integrations.gchat import GChatClient, select_new
from .registry import Tool, ToolRegistry


def register(registry: ToolRegistry, gchat: GChatClient) -> None:
    async def gchat_recent(query: str | None = None, limit: int = 15) -> str:
        limit = max(1, min(limit, 25))
        msgs = await asyncio.to_thread(gchat.recent_messages, None, 10)
        msgs = select_new(msgs, since=None, self_id=gchat.self_id)
        if query:
            q = query.lower()
            msgs = [
                m for m in msgs
                if q in m.get("text", "").lower() or q in m.get("sender_name", "").lower()
            ]
        warning = (
            "⚠️ Google Chat was only partially checked; one or more spaces "
            "were unavailable."
            if not gchat.last_read_complete
            else ""
        )
        if not msgs:
            empty = (
                "No matching inbound Google Chat messages in the spaces that responded."
                if query and warning
                else "No inbound Google Chat messages in the spaces that responded."
                if warning
                else "No matching inbound Google Chat messages."
                if query
                else "No recent inbound Google Chat messages."
            )
            return "\n".join(part for part in (warning, empty) if part)
        listing = "\n".join(
            f"{m.get('create_time', '')} — {m.get('sender_name') or m.get('sender', '?')}: {m.get('text', '')}"
            for m in msgs[:limit]
        )
        return "\n".join(part for part in (warning, listing) if part)

    registry.register(Tool(
        name="gchat_recent",
        description=(
            "Read the owner's recent Google Chat messages (their DMs and rooms). "
            "Call this before answering anything about who has messaged them on "
            "Chat. Pass `query` to filter by sender name or text; omit it for the "
            "latest across all spaces."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional: filter by sender or text."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Max messages to return (default 15, hard cap 25).",
                },
            },
        },
        func=gchat_recent,
    ))
