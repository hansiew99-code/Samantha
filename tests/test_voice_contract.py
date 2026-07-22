"""Voice guardrails for owner-facing prose and proactive prompts."""

from samantha.digests import (
    AFTERNOON_INSTRUCTIONS,
    EVENING_INSTRUCTIONS,
    MORNING_INSTRUCTIONS,
    DigestService,
)
from samantha.events import SWEEP_INSTRUCTIONS
from samantha.personality import SYSTEM_PROMPT


def test_personality_demands_text_message_cadence_not_report_voice():
    lowered = SYSTEM_PROMPT.casefold()

    assert "easy rhythm" in lowered
    assert "answer first" in lowered
    assert "one direct sentence or two short paragraphs" in lowered
    assert "don't open with a greeting, a recap, a count, or a report label" in lowered
    assert "treat follow-ups as part of the same conversation" in lowered
    assert "exact source" in lowered
    assert "samantha from" not in lowered
    assert "devil wears prada" not in lowered
    assert "you are both" not in lowered
    assert "lowercase is fine" not in lowered
    assert "my brain" not in lowered


def test_personality_limits_humour_and_rejects_fake_closeness():
    lowered = SYSTEM_PROMPT.casefold()

    assert "stakes are low" in lowered
    assert "one short aside per response at most" in lowered
    assert "if the joke needs effort, leave it out" in lowered
    assert "fake intimacy" in lowered
    assert "pet names" in lowered


def test_personality_replaces_corporate_copy_with_concrete_language():
    lowered = SYSTEM_PROMPT.casefold()

    for phrase in ("flagged", "blocker", "actioned", "leverage", "move forward"):
        assert phrase in lowered
    assert "facebook access still isn't fixed, so boosts can't run" in lowered
    assert "google chat — reanne sent it there" in lowered
    assert "today's send is waiting on them" in lowered
    assert "don't replace a known person with vague" in lowered


def test_personality_uses_obvious_meeting_context_and_specific_receipts():
    lowered = SYSTEM_PROMPT.casefold()

    assert "the only upcoming event with phillip" in lowered
    assert "use ten minutes before as the default" in lowered
    assert "what changed and when it will happen" in lowered
    assert "never answer only “done.” or “sorted.”" in lowered


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

    assert message == (
        "Client review is at 09:00 on your calendar. "
        "On Google Chat, Reanne sent the August brief."
    )
    assert "\n" not in message
    assert " — " not in message
    assert "token" not in message.casefold()
    assert "budget" not in message.casefold()
