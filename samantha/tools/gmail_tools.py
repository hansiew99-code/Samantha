"""gmail_* tools (BRIEF §8). Reading is free-form; sending goes through the
pending-action approval gate — the model can only ever draft.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from ..actions import PendingActions
from ..integrations.gmail import GmailClient
from .registry import Tool, ToolRegistry

NotifyDraft = Callable[[int, str], Awaitable[None]]  # (action_id, preview)


def register(
    registry: ToolRegistry,
    gmail: GmailClient,
    actions: PendingActions,
    notify_draft: NotifyDraft,
) -> None:
    async def gmail_search(query: str) -> str:
        msgs = await asyncio.to_thread(gmail.search, query)
        if not msgs:
            return "No matching emails."
        return "\n".join(
            f"[thread {m['thread_id']}] {m['date']} — {m['from']}: {m['subject']} — {m['snippet']}"
            for m in msgs
        )

    async def gmail_read_thread(thread_id: str) -> str:
        return await asyncio.to_thread(gmail.read_thread, thread_id)

    async def gmail_draft_reply(
        to: str, subject: str, body: str, thread_id: str | None = None
    ) -> str:
        preview = f"✉️ To: {to}\nSubject: {subject}\n\n{body}"
        action_id = actions.create(
            "gmail_send",
            {"to": to, "subject": subject, "body": body, "thread_id": thread_id},
            preview,
        )
        await notify_draft(action_id, preview)
        return (
            f"Draft #{action_id} sent to the owner for approval via Telegram. "
            "It is NOT sent yet — tell the owner it's waiting for their tap, "
            "and do not draft it again."
        )

    registry.register(Tool(
        name="gmail_search",
        description=(
            "Search the owner's Gmail with standard Gmail query syntax (e.g. "
            "'from:sarah is:unread', 'subject:invoice newer_than:7d'). Call "
            "this before answering anything about their email."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        func=gmail_search,
    ))

    registry.register(Tool(
        name="gmail_read_thread",
        description="Read a full email thread by thread id (from gmail_search results). Bodies are clipped for brevity.",
        input_schema={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
        },
        func=gmail_read_thread,
    ))

    registry.register(Tool(
        name="gmail_draft_reply",
        description=(
            "Draft an email for the owner's approval. This NEVER sends "
            "directly — the owner gets the draft in Telegram with a Send "
            "button. Use for replies (pass thread_id) and new emails alike. "
            "Write the body ready-to-send, in the owner's voice, no "
            "placeholders."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "thread_id": {"type": "string", "description": "Set when replying to keep Gmail threading."},
            },
            "required": ["to", "subject", "body"],
        },
        func=gmail_draft_reply,
    ))
