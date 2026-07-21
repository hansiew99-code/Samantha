"""Brain: tool loop, escalation, spend logging — all against the fake client."""

from __future__ import annotations

import pytest

from samantha.brain import Brain
from samantha.governor import Governor
from samantha.router import HAIKU, OPUS, SONNET
from samantha.tools import ToolRegistry
from samantha.tools import memory_tools

from fakes import FakeAnthropicClient, FakeResponse, text_block, tool_use


@pytest.fixture
def registry(memory) -> ToolRegistry:
    r = ToolRegistry()
    memory_tools.register(r, memory)
    return r


def make_brain(settings, memory, registry, conn, script) -> tuple[Brain, FakeAnthropicClient]:
    client = FakeAnthropicClient(script)
    governor = Governor(conn, settings.daily_budget_usd)
    return Brain(settings, memory, registry, governor, client=client), client


async def test_tool_loop_executes_and_replies(settings, memory, registry, conn):
    script = [
        FakeResponse(
            content=[tool_use("memory_save", {"subject": "owner", "predicate": "likes", "object": "satay"})],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block("Noted — satay it is.")]),
    ]
    brain, client = make_brain(settings, memory, registry, conn, script)

    reply = await brain.handle_message("remember I like satay")

    assert reply == "Noted — satay it is."
    assert memory.search_facts("satay likes")  # tool actually ran
    # Both API calls priced into the ledger.
    rows = conn.execute("SELECT COUNT(*) AS c, SUM(cost_usd) AS s FROM spend_log").fetchone()
    assert rows["c"] == 2 and rows["s"] > 0
    # Second call carried the tool result back.
    assert client.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    # Default routing: Haiku.
    assert client.calls[0]["model"] == HAIKU


async def test_escalation_switches_model_mid_loop(settings, memory, registry, conn):
    script = [
        FakeResponse(
            content=[tool_use("escalate", {"model": "sonnet", "reason": "tricky drafting"})],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block("Carefully drafted answer.")]),
    ]
    brain, client = make_brain(settings, memory, registry, conn, script)

    reply = await brain.handle_message("write something delicate")

    assert reply == "Carefully drafted answer."
    assert client.calls[0]["model"] == HAIKU
    assert client.calls[1]["model"] == SONNET
    models = [r["model"] for r in conn.execute("SELECT model FROM spend_log ORDER BY id")]
    assert models == [HAIKU, SONNET]


async def test_think_hard_routes_straight_to_opus(settings, memory, registry, conn):
    script = [FakeResponse(content=[text_block("deep answer")])]
    brain, client = make_brain(settings, memory, registry, conn, script)
    await brain.handle_message("think hard about my week please")
    assert client.calls[0]["model"] == OPUS


async def test_escalation_declined_when_capped(settings, memory, registry, conn):
    script = [
        FakeResponse(
            content=[tool_use("escalate", {"model": "opus", "reason": "x"})],
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block("stayed cheap")]),
    ]
    brain, client = make_brain(settings, memory, registry, conn, script)
    brain.max_tier = lambda: HAIKU  # governor cap (Phase 4 wires this for real)

    await brain.handle_message("hard thing")

    assert client.calls[1]["model"] == HAIKU  # never escalated
    result = client.calls[1]["messages"][-1]["content"][0]
    assert "declined" in result["content"]


async def test_tool_error_is_surfaced_not_fatal(settings, memory, registry, conn):
    script = [
        FakeResponse(
            content=[tool_use("memory_save", {"subject": "x"})],  # missing required args
            stop_reason="tool_use",
        ),
        FakeResponse(content=[text_block("recovered")]),
    ]
    brain, client = make_brain(settings, memory, registry, conn, script)
    reply = await brain.handle_message("save something malformed")
    assert reply == "recovered"
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result.get("is_error") is True


async def test_tool_specs_are_sorted_and_stable(settings, memory, registry, conn):
    script = [
        FakeResponse(content=[text_block("one")]),
        FakeResponse(content=[text_block("two")]),
    ]
    brain, client = make_brain(settings, memory, registry, conn, script)
    await brain.handle_message("first")
    await brain.handle_message("second")
    names1 = [t["name"] for t in client.calls[0]["tools"]]
    names2 = [t["name"] for t in client.calls[1]["tools"]]
    assert names1 == sorted(names1)
    assert names1 == names2  # byte-stable tool list across requests
