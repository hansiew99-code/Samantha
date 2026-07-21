"""Context assembly (BRIEF §5): every LLM call gets the same bounded package —

  [stable, cached]   system prompt + core memory        (~1-3K tokens)
  [volatile]         now/timezone + retrieved facts     (after the breakpoint)
  [messages]         last N turns + the user message

Cost per call is O(1) in total memory size: 10,000 stored facts cost the same
as 100, because only the FTS top-k ever enter the prompt.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .db import kv_get
from .memory import Memory
from .personality import SYSTEM_PROMPT

TURN_WINDOW = 20  # rows (~10 back-and-forth turns)
FACT_K = 8


def estimate_tokens(text: str) -> int:
    """Cheap offline heuristic (≈4 chars/token) for budget assertions/tests.
    Real accounting uses the API's usage block (governor.py)."""
    return len(text) // 4 + 1


def assemble(
    memory: Memory,
    user_message: str,
    tz: str,
    extra_volatile: str = "",
) -> tuple[list[dict], list[dict]]:
    """Returns (system_blocks, messages) for an Anthropic messages.create call.

    The cache_control breakpoint sits on the core-memory block: everything
    before and including it is byte-stable between calls; everything volatile
    comes after it or in messages.
    """
    core = memory.core_block()
    system_blocks: list[dict] = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {
            "type": "text",
            "text": f"# Core memory\n{core}",
            "cache_control": {"type": "ephemeral"},
        },
    ]

    now = datetime.now(ZoneInfo(tz))
    volatile_parts = [f"Current time: {now.strftime('%A %Y-%m-%d %H:%M')} ({tz})."]
    summary = kv_get(memory.conn, "conversation.summary")
    if summary:
        volatile_parts.append(f"Earlier-conversation summary: {summary}")
    facts = memory.search_facts(user_message, k=FACT_K)
    if facts:
        rendered = "\n".join(f"- {f.render()}" for f in facts)
        volatile_parts.append(f"Possibly relevant memories:\n{rendered}")
    if extra_volatile:
        volatile_parts.append(extra_volatile)
    system_blocks.append({"type": "text", "text": "\n\n".join(volatile_parts)})

    messages: list[dict] = [
        {"role": r["role"], "content": r["content"]}
        for r in memory.recent_messages(TURN_WINDOW)
    ]
    messages.append({"role": "user", "content": user_message})
    # The API requires the first message to be a user turn.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return system_blocks, messages


def total_estimated_tokens(system_blocks: list[dict], messages: list[dict]) -> int:
    text = "".join(b["text"] for b in system_blocks) + "".join(
        m["content"] if isinstance(m["content"], str) else str(m["content"]) for m in messages
    )
    return estimate_tokens(text)
