"""Context assembly: the O(1)-cost invariant and cache layout (BRIEF §5, §9)."""

from samantha.context import (
    FACT_TOKEN_BUDGET,
    RECENT_MESSAGE_TOKEN_BUDGET,
    SUMMARY_TOKEN_BUDGET,
    assemble,
    estimate_tokens,
    total_estimated_tokens,
)
from samantha.db import kv_set
from samantha.rules import RulesEngine


def test_context_bounded_with_10k_facts(memory, conn):
    """10,000 stored facts must not bloat the per-call context."""
    rows = [
        (f"topic-{i}", "relates to", f"a fairly long detail string about item number {i} " * 3)
        for i in range(10_000)
    ]
    conn.executemany(
        "INSERT INTO facts(subject, predicate, object) VALUES (?, ?, ?)", rows
    )
    conn.commit()

    system, messages = assemble(memory, "tell me about topic-4217", tz="Asia/Kuala_Lumpur")
    assert total_estimated_tokens(system, messages) < 6000


def test_many_standing_rules_cannot_bloat_every_prompt(memory, conn):
    rules = RulesEngine(conn, memory)
    for index in range(100):
        rules.add(
            "gmail",
            "suppress",
            scope=f"newsletter-{index}@example.com",
            detail="owner preference " * 100,
        )

    system, messages = assemble(memory, "what needs me?", tz="Asia/Kuala_Lumpur")

    assert total_estimated_tokens(system, messages) < 6_000
    # Prompt echo may be clipped, but deterministic enforcement remains exact.
    assert rules.allows("gmail", "newsletter-99@example.com") is False


def test_cache_breakpoint_on_stable_block_only(memory):
    system, _ = assemble(memory, "hello", tz="Asia/Kuala_Lumpur")
    # Block order: [prompt, core(+cache marker), volatile]. Volatile content
    # after the breakpoint must never carry cache_control.
    assert "cache_control" not in system[0]
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[2]
    # Volatile block carries the clock; stable blocks must not.
    assert "Current time" in system[2]["text"]
    assert "Current time" not in system[0]["text"] + system[1]["text"]


def test_stable_prefix_is_byte_identical_across_calls(memory):
    s1, _ = assemble(memory, "message one", tz="Asia/Kuala_Lumpur")
    s2, _ = assemble(memory, "a totally different message", tz="Asia/Kuala_Lumpur")
    assert s1[0]["text"] == s2[0]["text"]
    assert s1[1]["text"] == s2[1]["text"]


def test_conversation_window_included_and_bounded(memory):
    for i in range(100):
        memory.log_message("user", f"user msg {i}")
        memory.log_message("assistant", f"assistant msg {i}")
    _, messages = assemble(memory, "latest", tz="Asia/Kuala_Lumpur")
    assert messages[-1] == {"role": "user", "content": "latest"}
    assert len(messages) <= 21  # 20-row window + current message
    assert messages[0]["role"] == "user"


def test_variable_context_components_have_hard_token_caps(memory, conn):
    kv_set(conn, "conversation.summary", "summary-word " * 5_000)
    memory.save_fact(
        "needle-project",
        "notes are",
        "needle " + ("very-long-detail " * 5_000),
    )
    for i in range(20):
        memory.log_message("user", f"history-{i} " + ("pasted-content " * 5_000))

    system, messages = assemble(memory, "tell me about needle", tz="Asia/Kuala_Lumpur")

    history_tokens = sum(estimate_tokens(message["content"]) for message in messages[:-1])
    assert history_tokens <= RECENT_MESSAGE_TOKEN_BUDGET

    volatile = system[-1]["text"]
    summary = volatile.split("Earlier-conversation summary: ", 1)[1].split("\n\n", 1)[0]
    assert estimate_tokens(summary) <= SUMMARY_TOKEN_BUDGET
    rendered_facts = volatile.split("Possibly relevant memories:\n", 1)[1]
    assert estimate_tokens(rendered_facts) <= FACT_TOKEN_BUDGET


def test_proactive_pushes_cannot_evict_owner_dialogue(memory):
    memory.log_message("user", "The launch codename is Kestrel")
    memory.log_message("assistant", "Got it — Kestrel.", channel="telegram_reply")
    for index in range(25):
        memory.log_message(
            "assistant", f"proactive push {index}", channel="telegram_push"
        )

    system, messages = assemble(memory, "yes", tz="Asia/Kuala_Lumpur")

    assert any(m["content"] == "The launch codename is Kestrel" for m in messages)
    volatile = system[-1]["text"]
    assert "proactive push 24" in volatile  # latest push makes “yes” intelligible
    assert "proactive push 22" not in volatile  # push history is tightly capped
    combined = sum(estimate_tokens(m["content"]) for m in messages[:-1])
    push_block = volatile.split(
        "Recent proactive Telegram message(s) already delivered:\n", 1
    )[1]
    assert combined + estimate_tokens(push_block) <= RECENT_MESSAGE_TOKEN_BUDGET


def test_answered_push_drops_out_of_later_prompts(memory):
    memory.log_message("assistant", "Approve the Rebecca reply?", channel="telegram_push")
    immediate_system, _ = assemble(memory, "yes", tz="Asia/Kuala_Lumpur")
    assert "Approve the Rebecca reply?" in immediate_system[-1]["text"]

    memory.log_message("user", "yes", channel="telegram")
    memory.log_message("assistant", "Sent ✓", channel="telegram_callback")
    later_system, _ = assemble(memory, "what next?", tz="Asia/Kuala_Lumpur")

    assert "Approve the Rebecca reply?" not in later_system[-1]["text"]


def test_long_latest_assistant_reply_keeps_preceding_user_turn(memory):
    memory.log_message("user", "Plan the Kestrel launch in detail")
    memory.log_message(
        "assistant",
        "Kestrel plan section " * 5_000,
        channel="telegram_reply",
    )

    _, messages = assemble(memory, "continue", tz="Asia/Kuala_Lumpur")

    assert messages[0]["role"] == "user"
    assert "Kestrel launch" in messages[0]["content"]
    assert messages[1]["role"] == "assistant"
    assert messages[-1] == {"role": "user", "content": "continue"}
    assert sum(estimate_tokens(m["content"]) for m in messages[:-1]) <= (
        RECENT_MESSAGE_TOKEN_BUDGET
    )
