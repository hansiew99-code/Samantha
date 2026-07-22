"""Voice guardrails for owner-facing prose and proactive prompts."""

from samantha.digests import (
    AFTERNOON_INSTRUCTIONS,
    EVENING_INSTRUCTIONS,
    MORNING_INSTRUCTIONS,
    DigestService,
)
from samantha.events import SWEEP_INSTRUCTIONS
from samantha.personality import SYSTEM_PROMPT


def test_personality_is_a_behavior_contract_not_character_cosplay():
    lowered = SYSTEM_PROMPT.casefold()

    assert "emotionally perceptive" in lowered
    assert "operationally formidable" in lowered
    assert "lead with the answer" in lowered
    assert "exact source" in lowered
    assert "samantha from" not in lowered
    assert "devil wears prada" not in lowered
    assert "you are both" not in lowered
    assert "lowercase is fine" not in lowered
    assert "my brain" not in lowered


def test_proactive_prompts_require_source_consequence_and_priority():
    sweep = SWEEP_INSTRUCTIONS.casefold()
    morning = MORNING_INSTRUCTIONS.casefold()
    afternoon = AFTERNOON_INSTRUCTIONS.casefold()
    evening = EVENING_INSTRUCTIONS.casefold()

    assert "exact source" in sweep
    assert "why the event matters" in sweep
    assert "rank the first move" in morning
    assert "name the exact source" in afternoon
    assert "name the exact source" in evening


def test_plain_digest_names_sources_without_internal_system_copy():
    message = DigestService._plain_digest(
        {
            "calendar_next_48h": [
                {"start": "09:00", "summary": "Client review"}
            ],
            "open_tasks": [
                {"source": "clickup", "title": "Send deck", "due_at": "today"}
            ],
            "backlog": [
                {"source": "gchat", "summary": "Reanne sent the August brief"}
            ],
        }
    )

    assert message.splitlines() == [
        "Calendar — 09:00: Client review",
        "ClickUp — Send deck (due today)",
        "Google Chat — Reanne sent the August brief",
    ]
    assert "token" not in message.casefold()
    assert "budget" not in message.casefold()
