"""gchat_* tools (read-only). Google Chat has no send path here — the brain can
look at what people have said, but replying to Chat isn't wired (v1)."""

from __future__ import annotations

import asyncio

from ..integrations.gchat import GChatClient
from .registry import Tool, ToolRegistry


def register(registry: ToolRegistry, gchat: GChatClient) -> None:
    async def gchat_recent(query: str | None = None, limit: int = 15) -> str:
        msgs = await asyncio.to_thread(gchat.recent_messages, None, 10)
        msgs = [m for m in msgs if m.get("text")]
        if query:
            q = query.lower()
            msgs = [
                m for m in msgs
                if q in m.get("text", "").lower() or q in m.get("sender_name", "").lower()
            ]
        msgs.sort(key=lambda m: m.get("create_time", ""), reverse=True)
        if not msgs:
            return "No matching Google Chat messages." if query else "No recent Google Chat messages."
        return "\n".join(
            f"{m.get('create_time', '')} — {m.get('sender_name') or m.get('sender', '?')}: {m.get('text', '')}"
            for m in msgs[:limit]
        )

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
                "limit": {"type": "integer", "description": "Max messages to return (default 15)."},
            },
        },
        func=gchat_recent,
    ))
