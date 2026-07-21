"""rules_* tools (BRIEF §7): the mechanism behind "stop reminding me about X"."""

from __future__ import annotations

from ..rules import RulesEngine
from .registry import Tool, ToolRegistry


def register(registry: ToolRegistry, rules: RulesEngine) -> None:
    def rules_add(source: str, action: str, scope: str = "*", detail: str | None = None) -> str:
        rid = rules.add(source, action, scope, detail)
        return (
            f"Rule #{rid} saved and now enforced permanently: {action} {source}"
            + (f" (scope: {scope})" if scope != "*" else " (everything)")
        )

    def rules_remove(rule_id: int) -> str:
        ok = rules.deactivate(int(rule_id))
        return f"Rule #{rule_id} removed." if ok else f"No active rule #{rule_id}."

    def rules_list() -> str:
        active = rules.active_rules()
        if not active:
            return "No standing rules."
        return "\n".join(
            f"#{r['id']}: {r['action']} {r['source']} (scope: {r['scope']})"
            + (f" — {r['detail']}" if r["detail"] else "")
            for r in active
        )

    registry.register(Tool(
        name="rules_add",
        description=(
            "Persist a standing behavior rule. Call this WHENEVER the owner "
            "tells you to change how you behave going forward: 'stop "
            "reminding me about ClickUp' → source=clickup action=suppress; "
            "'mute the #general channel' → source=slack action=suppress "
            "scope=general; 'always flag emails from my boss' → source=gmail "
            "action=vip scope=<boss email>. Rules are enforced in code "
            "forever, before any event reaches you — a mere acknowledgement "
            "in chat would be forgotten."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["gmail", "slack", "clickup", "calendar", "*"]},
                "action": {"type": "string", "enum": ["suppress", "vip"]},
                "scope": {"type": "string", "description": "Sender email, channel, list name, or * for everything from the source."},
                "detail": {"type": "string", "description": "Optional human-readable note about why."},
            },
            "required": ["source", "action"],
        },
        func=rules_add,
    ))

    registry.register(Tool(
        name="rules_remove",
        description="Deactivate a standing rule by id (use rules_list first). Call when the owner reverses an earlier instruction.",
        input_schema={
            "type": "object",
            "properties": {"rule_id": {"type": "integer"}},
            "required": ["rule_id"],
        },
        func=rules_remove,
    ))

    registry.register(Tool(
        name="rules_list",
        description="List all active standing rules. Call when the owner asks what's muted/flagged or before removing one.",
        input_schema={"type": "object", "properties": {}},
        func=rules_list,
    ))
