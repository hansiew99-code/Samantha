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

# Anthropic strict tool schemas use the structured-output schema subset.  Keep
# range/shape constraints in descriptions and enforce them in tool code; these
# keywords are rejected (or silently weakened by SDK transforms) by that
# subset.  The provider bundle validator below keeps an invalid schema from
# taking every interactive reply offline.
UNSUPPORTED_PROVIDER_SCHEMA_KEYWORDS = frozenset({
    "contains",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "maxContains",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minContains",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "pattern",
    "patternProperties",
    "propertyNames",
    "uniqueItems",
})

MAX_STRICT_TOOLS = 20
MAX_STRICT_OPTIONAL_PARAMETERS = 24
MAX_STRICT_UNION_PARAMETERS = 16

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

# Strict grammar compilation is deliberately narrower than mutation policy.
# Anthropic applies a combined grammar-complexity ceiling even when the public
# 20/24/16 limits are satisfied.  Reserve it for provider-facing or destructive
# payloads that can immediately change an external provider.  Local,
# reversible mutations and approval-gated drafts still pass through owner-
# authorization, deterministic Python validation, and (for drafts) an exact
# Telegram preview before external execution.
STRICT_TOOLS = frozenset({
    "calendar_create_event",
    "calendar_update_event",
    "clickup_complete_task",
    "clickup_update_task",
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
        # Strict tool use is reserved for side-effecting tools.  Anthropic caps
        # the number and total complexity of strict schemas, while read-only
        # tools are safely bounded by their implementations.  Keep the schema
        # copy local so registration remains immutable (and therefore
        # byte-stable for prompt caching).
        schema = dict(self.input_schema)
        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)
        spec = {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }
        if self.name in STRICT_TOOLS:
            spec["strict"] = True
        return spec


def validate_provider_tools(tools: list[dict]) -> None:
    """Validate a final Anthropic tool bundle before making a model request.

    Strict tool use shares Anthropic's structured-output limits.  A single
    invalid schema rejects the entire request, so validate the assembled bundle
    (including any tools added outside :class:`ToolRegistry`) at the boundary.
    """
    strict_tools = [tool for tool in tools if tool.get("strict") is True]
    if len(strict_tools) > MAX_STRICT_TOOLS:
        names = ", ".join(str(tool.get("name", "<unnamed>")) for tool in strict_tools)
        raise ValueError(
            "Anthropic strict-tool limit exceeded: "
            f"{len(strict_tools)} tools (maximum {MAX_STRICT_TOOLS}). "
            f"Keep strict mode only on mutation-critical tools. Strict tools: {names}"
        )

    optional_count = 0
    union_count = 0
    for tool in tools:
        name = str(tool.get("name", "<unnamed>"))
        schema = tool.get("input_schema", {})
        if not isinstance(schema, dict):
            raise ValueError(f"Tool {name!r} input_schema must be an object")

        invalid = _find_unsupported_schema_keyword(schema)
        if invalid is not None:
            path, keyword = invalid
            raise ValueError(
                f"Tool {name!r} uses unsupported Anthropic schema keyword "
                f"{keyword!r} at input_schema{path}. Move the constraint into "
                "the parameter description and enforce it in tool code."
            )

        if tool.get("strict") is True:
            optional_count += _count_optional_parameters(schema)
            union_count += _count_union_parameters(schema)

    if optional_count > MAX_STRICT_OPTIONAL_PARAMETERS:
        raise ValueError(
            "Anthropic strict-schema optional-parameter limit exceeded: "
            f"{optional_count} parameters (maximum "
            f"{MAX_STRICT_OPTIONAL_PARAMETERS}). Make parameters required or "
            "remove strict mode from non-critical tools."
        )
    if union_count > MAX_STRICT_UNION_PARAMETERS:
        raise ValueError(
            "Anthropic strict-schema union-parameter limit exceeded: "
            f"{union_count} parameters (maximum {MAX_STRICT_UNION_PARAMETERS}). "
            "Simplify union schemas or remove strict mode from non-critical tools."
        )


def _find_unsupported_schema_keyword(
    value: Any,
    path: str = "",
) -> tuple[str, str] | None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in UNSUPPORTED_PROVIDER_SCHEMA_KEYWORDS:
                return child_path, key
            # Anthropic supports minItems only for zero or one.
            if key == "minItems" and child not in (0, 1):
                return child_path, key
            found = _find_unsupported_schema_keyword(child, child_path)
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_unsupported_schema_keyword(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _count_optional_parameters(schema: Any) -> int:
    if isinstance(schema, dict):
        count = 0
        properties = schema.get("properties")
        if isinstance(properties, dict):
            required = schema.get("required", [])
            required_names = set(required) if isinstance(required, list) else set()
            count += sum(name not in required_names for name in properties)
        count += sum(_count_optional_parameters(child) for child in schema.values())
        return count
    if isinstance(schema, list):
        return sum(_count_optional_parameters(child) for child in schema)
    return 0


def _count_union_parameters(schema: Any) -> int:
    if isinstance(schema, dict):
        count = int(
            "anyOf" in schema
            or "oneOf" in schema
            or isinstance(schema.get("type"), list)
        )
        count += sum(_count_union_parameters(child) for child in schema.values())
        return count
    if isinstance(schema, list):
        return sum(_count_union_parameters(child) for child in schema)
    return 0


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
