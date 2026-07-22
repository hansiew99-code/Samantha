"""Offline contracts for grounded source answers and failure continuity.

These cases intentionally exercise the screenshot failure: Samantha had just
surfaced Reanne's August brief, was asked whether it came from Chat or Gmail,
and lost both the answer and the referent when the model call failed.
"""

from __future__ import annotations

import json

from samantha.brain import Brain
from samantha.context import assemble
from samantha.governor import Governor
from samantha.tools import ToolRegistry

from fakes import FakeAnthropicClient


def _reply_text(reply: object) -> str:
    """Accept today's string API and a future typed owner-reply envelope."""
    text = getattr(reply, "text", reply)
    assert isinstance(text, str)
    return text


async def test_recent_gchat_provenance_answers_source_question_without_tokens(
    settings, memory, conn
):
    event_id = conn.execute(
        "INSERT INTO events_queue(source, kind, scope, payload, processed_at, disposition) "
        "VALUES (?, ?, ?, ?, datetime('now'), 'notified')",
        (
            "gchat",
            "new_message",
            "Reanne",
            json.dumps(
                {
                    "sender_name": "Reanne",
                    "text": "The August content brief is ready for review.",
                    "create_time": "2026-07-22T09:10:00Z",
                }
            ),
        ),
    ).lastrowid
    assert event_id is not None
    conn.commit()
    memory.log_message(
        "assistant",
        "Reanne's August content brief landed.",
        channel="telegram_push",
    )

    # An empty script makes any Anthropic call fail the test. Source provenance
    # is already local and should cost zero model tokens.
    client = FakeAnthropicClient([])
    brain = Brain(
        settings,
        memory,
        ToolRegistry(),
        Governor(conn, settings.daily_budget_usd),
        client=client,
    )

    reply = await brain.handle_message("Reanne's August brief is on Chat or Gmail?")

    text = _reply_text(reply).casefold()
    assert "google chat" in text
    assert "gmail" not in text or "not gmail" in text
    assert client.calls == []


def test_error_reply_does_not_acknowledge_push_but_successful_reply_does(
    memory,
):
    push = "Reanne's August content brief landed."
    memory.log_message("assistant", push, channel="telegram_push")
    memory.log_message(
        "user", "Was that on Chat or Gmail?", channel="telegram"
    )
    memory.log_message(
        "assistant",
        "I can't run that lookup right now.",
        channel="telegram_error",
    )

    # A failed response did not answer the owner's question. The push remains
    # the live referent for an immediate retry, even though both a user row and
    # an error row were persisted after it.
    system, _ = assemble(
        memory,
        "Reanne's August brief is on Chat or Gmail?",
        tz="Asia/Kuala_Lumpur",
    )
    assert push in system[-1]["text"]
    assert [row["content"] for row in memory.recent_proactive_messages()] == [push]

    memory.log_message(
        "assistant",
        "Google Chat — Reanne sent it there.",
        channel="telegram_reply",
    )

    # A successfully delivered semantic answer closes the referent.
    later_system, _ = assemble(memory, "what next?", tz="Asia/Kuala_Lumpur")
    assert push not in later_system[-1]["text"]
    assert memory.recent_proactive_messages() == []


async def test_model_failure_copy_is_specific_degraded_and_never_transfers_retry(
    settings, memory, conn
):
    client = FakeAnthropicClient([RuntimeError("provider unavailable")])
    brain = Brain(
        settings,
        memory,
        ToolRegistry(),
        Governor(conn, settings.daily_budget_usd),
        client=client,
    )

    reply = await brain.handle_message("Where did Reanne's brief come from?")

    text = _reply_text(reply).casefold()
    assert "failed on my side" in text
    assert "nothing changed" in text
    assert "brain" not in text
    assert "try me" not in text
    assert "ask me again" not in text
    assert "send it again" not in text

    # If the production reply API has become typed, the failure must carry an
    # explicit degraded state rather than relying on prose parsing.
    status = getattr(reply, "status", None)
    if status is not None:
        assert str(getattr(status, "value", status)).casefold() == "degraded"
