"""Digests: one LLM call, deterministic gathering, backlog consumption."""

from __future__ import annotations

import pytest

from samantha.brain import Brain
from samantha.digests import DigestService
from samantha.events import EventBus
from samantha.governor import Governor
from samantha.router import HAIKU, SONNET
from samantha.rules import RulesEngine
from samantha.tools import ToolRegistry

from fakes import FakeAnthropicClient, FakeResponse, text_block


@pytest.fixture
def bus(conn, memory) -> EventBus:
    return EventBus(conn, RulesEngine(conn, memory))


def make_digests(settings, conn, memory, bus, script, notifications):
    client = FakeAnthropicClient(script)
    brain = Brain(settings, memory, ToolRegistry(), Governor(conn, 1.0), client=client)

    async def notify(text: str) -> None:
        notifications.append(text)

    return DigestService(settings, memory, bus, brain, notify, gcal=None, conn=conn), client


async def test_morning_digest_one_sonnet_call(settings, conn, memory, bus):
    conn.execute("INSERT INTO tasks(source, title, due_at) VALUES ('clickup', 'Ship report', '2026-07-22')")
    conn.commit()
    bus.enqueue("gmail", "new_email", "a@b.c", {"subject": "Invoice"})
    for ev in bus.pending():
        bus.mark(ev["id"], "digest")

    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Morning! One task due, one email waiting.")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == SONNET
    assert notifications == ["Morning! One task due, one email waiting."]
    # The gathered data reached the model.
    payload = client.calls[0]["messages"][0]["content"]
    assert "Ship report" in payload and "Invoice" in payload


async def test_evening_nothing_suppresses_push(settings, conn, memory, bus):
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("NOTHING")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.evening()

    assert client.calls[0]["model"] == HAIKU
    assert notifications == []  # nothing useful → no ping (protect attention)


async def test_digest_consumes_pending_backlog(settings, conn, memory, bus):
    bus.enqueue("slack", "mention", "C123", {"text": "ping"})
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Brief.")])]
    digests, _ = make_digests(settings, conn, memory, bus, script, notifications)

    await digests.morning()

    assert bus.pending() == []  # swept into the digest, marked processed
