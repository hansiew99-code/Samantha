"""memory_* tools (BRIEF §8)."""

from __future__ import annotations

from ..memory import Memory
from .registry import Tool, ToolRegistry


def register(registry: ToolRegistry, memory: Memory) -> None:
    def memory_save(
        subject: str,
        predicate: str,
        object: str,  # noqa: A002
        replace_existing: bool = False,
    ) -> str:
        fact_id = memory.save_fact(
            subject, predicate, object, replace_existing=replace_existing
        )
        return f"Saved fact #{fact_id}: {subject} {predicate} {object}"

    def memory_search(query: str) -> str:
        facts = memory.search_facts(query, k=10)
        messages = memory.search_messages(query, k=6)
        if not facts and not messages:
            return "No stored memories match."
        sections: list[str] = []
        if facts:
            sections.append(
                "Stored facts:\n"
                + "\n".join(f"- {f.render()} (source: {f.source})" for f in facts)
            )
        if messages:
            sections.append(
                "Matching conversation excerpts (newest first):\n"
                + "\n".join(
                    f"- {row['created_at']} {row['role']}: "
                    f"{row['content'][:500]}"
                    for row in messages
                )
            )
        return "\n\n".join(sections)

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
            "By default facts accumulate, because a person can like or work on "
            "several things. Set `replace_existing=true` only for a genuine "
            "correction to one current value (timezone, employer, a changed "
            "deadline). "
            "Example: subject='owner', predicate='prefers', object='meetings "
            "after 10am'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Who/what the fact is about; use 'owner' for the user."},
                "predicate": {"type": "string", "description": "Short verb phrase, e.g. 'prefers', 'works at', 'is allergic to'."},
                "object": {"type": "string", "description": "The value of the fact."},
                "replace_existing": {
                    "type": "boolean",
                    "description": "True only when this fact replaces the previous value for the same subject and predicate.",
                },
            },
            "required": ["subject", "predicate", "object"],
        },
        func=memory_save,
    ))

    registry.register(Tool(
        name="memory_search",
        description=(
            "Search long-term facts and the durable conversation ledger beyond "
            "the auto-injected snippets. Call "
            "this when the owner references something from the past that is "
            "not in your current context, including earlier today, before saying "
            "you don't know. Results are bounded excerpts, not the whole history."
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
