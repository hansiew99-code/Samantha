"""The current user message must reach Claude exactly once (not twice)."""

from samantha.brain import Brain
from samantha.governor import Governor
from samantha.tools import ToolRegistry

from fakes import FakeAnthropicClient, FakeResponse, text_block


async def test_current_message_sent_once(settings, memory, conn):
    client = FakeAnthropicClient([FakeResponse(content=[text_block("hi there")])])
    brain = Brain(settings, memory, ToolRegistry(), Governor(conn, 1.0), client=client)

    await brain.handle_message("hello samantha")

    sent = client.calls[0]["messages"]
    dupes = [m for m in sent if m.get("content") == "hello samantha"]
    assert len(dupes) == 1, f"message sent {len(dupes)}x — token waste"

    # History stores it once, and a later turn sees exactly one prior copy.
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE role='user' AND content='hello samantha'"
    ).fetchone()
    assert rows["c"] == 1


async def test_prior_turns_appear_once_as_history(settings, memory, conn):
    client = FakeAnthropicClient([
        FakeResponse(content=[text_block("first reply")]),
        FakeResponse(content=[text_block("second reply")]),
    ])
    brain = Brain(settings, memory, ToolRegistry(), Governor(conn, 1.0), client=client)

    await brain.handle_message("first message")
    await brain.handle_message("second message")

    second_call = client.calls[1]["messages"]
    first_copies = [m for m in second_call if m.get("content") == "first message"]
    assert len(first_copies) == 1  # prior turn present exactly once
    assert second_call[-1] == {"role": "user", "content": "second message"}
