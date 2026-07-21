"""Nightly consolidation: fact extraction, core refresh, cursor, batch pricing."""

from __future__ import annotations

import json

import pytest

from samantha.consolidation import CURSOR_KEY, SUMMARY_KEY, Consolidator
from samantha.db import kv_get
from samantha.governor import Governor, Usage



@pytest.fixture
def consolidator(settings, conn, memory):
    gov = Governor(conn, settings.daily_budget_usd)
    return Consolidator(settings, conn, memory, gov, client=object())  # _submit is patched


def fake_results(facts=None, summary="Owner planning a trip.", core=None):
    usage = Usage(input_tokens=5000, output_tokens=300)
    return {
        "facts": (json.dumps({"facts": facts or []}), usage),
        "summary": (summary, usage),
        "core": (json.dumps(core or {}), usage),
    }


async def test_consolidation_end_to_end(consolidator, memory, conn, monkeypatch):
    memory.log_message("user", "I'm vegetarian now, remember that")
    memory.log_message("assistant", "Noted!")

    async def fake_submit(_requests):
        return fake_results(
            facts=[{"subject": "owner", "predicate": "diet is", "object": "vegetarian"}],
            core={"identity": "Owner: Han, in KL.", "preferences": "Vegetarian.",
                  "current_context": "Planning a trip."},
        )

    monkeypatch.setattr(consolidator, "_submit", fake_submit)
    ran = await consolidator.run()

    assert ran is True
    assert memory.search_facts("vegetarian diet")
    assert kv_get(conn, SUMMARY_KEY) == "Owner planning a trip."
    core = memory.core_block()
    assert "Vegetarian" in core and "Han" in core
    # Cursor advanced: a second run with no new messages does nothing.
    assert int(kv_get(conn, CURSOR_KEY)) >= 2
    assert await consolidator.run() is False


async def test_consolidation_supersedes_via_memory(consolidator, memory, monkeypatch):
    memory.save_fact("owner", "diet is", "omnivore")
    memory.log_message("user", "vegetarian now")

    async def fake_submit(_requests):
        return fake_results(facts=[{"subject": "owner", "predicate": "diet is", "object": "vegetarian"}])

    monkeypatch.setattr(consolidator, "_submit", fake_submit)
    await consolidator.run()

    objects = [f.object for f in memory.search_facts("owner diet vegetarian omnivore")]
    assert "vegetarian" in objects and "omnivore" not in objects


async def test_consolidation_priced_as_batch(consolidator, conn, memory, monkeypatch):
    memory.log_message("user", "hi")

    async def fake_submit(_requests):
        return fake_results()

    monkeypatch.setattr(consolidator, "_submit", fake_submit)
    await consolidator.run()

    rows = conn.execute(
        "SELECT batch, cost_usd FROM spend_log WHERE purpose = 'consolidation'"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["batch"] == 1 for r in rows)
    # 5000 in + 300 out on Haiku at 50%: (0.005 + 0.0015) * 0.5
    assert rows[0]["cost_usd"] == pytest.approx(0.00325)


async def test_unparseable_core_keeps_current_block(consolidator, memory, monkeypatch):
    memory.set_core("identity", "Existing identity")
    memory.log_message("user", "hello")

    async def fake_submit(_requests):
        usage = Usage(input_tokens=100, output_tokens=10)
        return {"core": ("not json at all", usage)}

    monkeypatch.setattr(consolidator, "_submit", fake_submit)
    await consolidator.run()
    assert "Existing identity" in memory.core_block()


async def test_batch_failure_does_not_advance_cursor(consolidator, conn, memory, monkeypatch):
    memory.log_message("user", "important thing")

    async def failing_submit(_requests):
        raise TimeoutError("batch stuck")

    monkeypatch.setattr(consolidator, "_submit", failing_submit)
    assert await consolidator.run() is False
    assert kv_get(conn, CURSOR_KEY) is None  # nothing lost — retried tomorrow
