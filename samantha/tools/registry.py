"""Tool registry. The spec list is sorted by name and byte-identical between
requests — a varying tool set would invalidate the whole prompt cache
(tools render at position 0 of the prefix).
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 4000  # truncate tool results — context is money

# Deterministic provenance policy. Results from these readers can contain text
# written by third parties. Once one has entered a tool loop, it must not gain
# new authority to invoke private mutations in a later model round.
UNTRUSTED_OUTPUT_TOOLS = frozenset({
    "gmail_search",
    "gmail_read_thread",
    "gchat_recent",
    "slack_recent",
    "clickup_list_tasks",
    "calendar_list_events",
    "calendar_find_slots",
    # This can replay owner-pasted or integration-derived text from durable
    # history; provenance is not equivalent to current Telegram authority.
    "memory_search",
})

MUTATING_TOOLS = frozenset({
    "memory_save",
    "people_save",
    "reminders_set",
    "reminders_cancel",
    "rules_add",
    "rules_remove",
    "watchers_set_email",
    "watchers_cancel",
    "clickup_complete_task",
    "clickup_update_task",
    "calendar_create_event",
    "calendar_update_event",
    "calendar_delete_event",
    "gmail_draft_reply",
    "slack_draft_reply",
})

# These only create an immutable local draft; the verified Telegram owner must
# still approve its exact payload before any external effect.
APPROVAL_GATED_TOOLS = frozenset({
    "gmail_draft_reply",
    "slack_draft_reply",
    # This tool only fetches the event preview and creates a local pending
    # action. The provider deletion happens later, after the owner's tap.
    "calendar_delete_event",
})


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    func: Callable[..., Any]  # sync or async; kwargs from tool input

    def spec(self) -> dict:
        # Strict tool use prevents malformed or partially-shaped arguments from
        # reaching integrations.  Keep the schema copy local so registration
        # remains immutable (and therefore byte-stable for prompt caching).
        schema = dict(self.input_schema)
        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def specs(self) -> list[dict]:
        return [self._tools[name].spec() for name in sorted(self._tools)]

    @staticmethod
    def is_untrusted_output(name: str) -> bool:
        return name in UNTRUSTED_OUTPUT_TOOLS

    @staticmethod
    def is_mutating(name: str) -> bool:
        return name in MUTATING_TOOLS

    @staticmethod
    def is_approval_gated(name: str) -> bool:
        return name in APPROVAL_GATED_TOOLS

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    async def execute(self, name: str, tool_input: dict) -> tuple[str, bool]:
        """Returns (content, is_error). Exceptions become error results the
        model can recover from rather than crashing the loop."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}", True
        try:
            result = tool.func(**(tool_input or {}))
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            if len(result) > MAX_RESULT_CHARS:
                result = result[:MAX_RESULT_CHARS] + "\n[truncated]"
            return result, False
        except Exception as exc:  # noqa: BLE001 — surface to the model
            log.exception("tool %s failed", name)
            return f"Error in {name}: {exc}", True
