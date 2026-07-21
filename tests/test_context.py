"""Context assembly: the O(1)-cost invariant and cache layout (BRIEF §5, §9)."""

from samantha.context import assemble, total_estimated_tokens


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
