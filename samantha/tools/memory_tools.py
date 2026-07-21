"""memory_* tools (BRIEF §8)."""

from __future__ import annotations

from ..memory import Memory
from .registry import Tool, ToolRegistry


def register(registry: ToolRegistry, memory: Memory) -> None:
    def memory_save(subject: str, predicate: str, object: str) -> str:  # noqa: A002
        fact_id = memory.save_fact(subject, predicate, object)
        return f"Saved fact #{fact_id}: {subject} {predicate} {object}"

    def memory_search(query: str) -> str:
        facts = memory.search_facts(query, k=10)
        if not facts:
            return "No stored memories match."
        return "\n".join(f"- {f.render()} (source: {f.source})" for f in facts)

    def people_save(
        name: str,
        email: str | None = None,
        relationship: str | None = None,
        timezone: str | None = None,
        notes: str | None = None,
        vip: bool | None = None,
    ) -> str:
        pid = memory.save_person(
            name, email=email, relationship=relationship, timezone=timezone,
            notes=notes, vip=int(vip) if vip is not None else None,
        )
        return f"Saved person #{pid}: {name}"

    registry.register(Tool(
        name="memory_save",
        description=(
            "Store a durable fact about the owner or their world. Call this "
            "whenever the owner states something worth remembering — a "
            "preference, a plan, a deadline, a relationship, a habit. Facts "
            "with the same subject+predicate supersede the old value. "
            "Example: subject='owner', predicate='prefers', object='meetings "
            "after 10am'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Who/what the fact is about; use 'owner' for the user."},
                "predicate": {"type": "string", "description": "Short verb phrase, e.g. 'prefers', 'works at', 'is allergic to'."},
                "object": {"type": "string", "description": "The value of the fact."},
            },
            "required": ["subject", "predicate", "object"],
        },
        func=memory_save,
    ))

    registry.register(Tool(
        name="memory_search",
        description=(
            "Search long-term memory beyond the auto-injected snippets. Call "
            "this when the owner references something from the past that is "
            "not in your current context, before saying you don't know."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        func=memory_search,
    ))

    registry.register(Tool(
        name="people_save",
        description=(
            "Create or update a contact card. Call this when you learn who "
            "someone is — their email, relationship to the owner, timezone, "
            "or that they matter (vip)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "relationship": {"type": "string"},
                "timezone": {"type": "string"},
                "notes": {"type": "string"},
                "vip": {"type": "boolean"},
            },
            "required": ["name"],
        },
        func=people_save,
    ))
