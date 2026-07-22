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


class FakeGmail:
    """Just enough of GmailClient for the digest gather step."""

    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        self.queries.append(query)
        return self.results


class FakeChat:
    """Just enough of GChatClient for the digest gather step."""

    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.calls: list[str] = []

    def recent_inbound(self, since_iso: str, per_space: int = 5) -> list[dict]:
        self.calls.append(since_iso)
        return self.results


def make_digests(settings, conn, memory, bus, script, notifications, gmail=None, gchat=None):
    client = FakeAnthropicClient(script)
    brain = Brain(settings, memory, ToolRegistry(), Governor(conn, 1.0), client=client)

    async def notify(text: str) -> None:
        notifications.append(text)

    return (
        DigestService(
            settings, memory, bus, brain, notify,
            gcal=None, gmail=gmail, gchat=gchat, conn=conn,
        ),
        client,
    )


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


async def test_morning_brief_actively_pulls_unread_email(settings, conn, memory, bus):
    # The failure the owner complained about: a brief that never checks the
    # inbox. The digest must query Gmail and feed the unread mail to the model.
    gmail = FakeGmail(
        [{"from": "sarah@x.com", "subject": "Deck review", "snippet": "can you look today?"}]
    )
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Morning — Sarah's still waiting on the deck.")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications, gmail=gmail)

    await digests.morning()

    assert gmail.queries and "is:unread" in gmail.queries[0]  # inbox actually checked
    payload = client.calls[0]["messages"][0]["content"]
    assert "Deck review" in payload  # and the unread mail reached the model


async def test_morning_brief_actively_reads_google_chat(settings, conn, memory, bus):
    # "Read the chat without me prompting" — the brief must query Chat on its
    # own and feed what it finds to the model, exactly like it does for Gmail.
    chat = FakeChat([{"sender_name": "Marcus", "text": "you around later?", "create_time": "2026-07-22T08:00:00Z"}])
    notifications: list[str] = []
    script = [FakeResponse(content=[text_block("Morning — Marcus pinged you on Chat.")])]
    digests, client = make_digests(settings, conn, memory, bus, script, notifications, gchat=chat)

    await digests.morning()

    assert chat.calls  # Chat was actually read, unprompted
    payload = client.calls[0]["messages"][0]["content"]
    assert "you around later?" in payload  # and the message reached the model


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
