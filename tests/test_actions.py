"""Pending-action approval gate: draft → tap → send exactly once (BRIEF §8)."""

from __future__ import annotations

import pytest

from samantha.actions import PendingActions
from samantha.tools import ToolRegistry
from samantha.tools import gmail_tools


@pytest.fixture
def actions(conn) -> PendingActions:
    return PendingActions(conn)


async def test_approve_executes_exactly_once(actions):
    sent = []

    async def executor(payload: dict) -> str:
        sent.append(payload)
        return "Sent ✓"

    actions.register_executor("gmail_send", executor)
    aid = actions.create("gmail_send", {"to": "a@b.c"}, "preview")

    assert await actions.approve(aid) == "Sent ✓"
    # Double-tap: must not send twice.
    result = await actions.approve(aid)
    assert "already" in result
    assert len(sent) == 1


async def test_discard_prevents_send(actions):
    async def executor(_payload: dict) -> str:
        raise AssertionError("must never execute")

    actions.register_executor("gmail_send", executor)
    aid = actions.create("gmail_send", {"to": "a@b.c"}, "preview")

    assert actions.discard(aid) is True
    result = await actions.approve(aid)
    assert "already" in result and "discarded" in result


async def test_failed_executor_is_quarantined_as_uncertain(actions, conn):
    async def executor(_payload: dict) -> str:
        raise RuntimeError("smtp exploded")

    actions.register_executor("gmail_send", executor)
    aid = actions.create("gmail_send", {"to": "a@b.c"}, "preview")
    result = await actions.approve(aid)
    assert "may have gone through" in result.lower()
    row = conn.execute("SELECT status FROM pending_actions WHERE id = ?", (aid,)).fetchone()
    assert row["status"] == "uncertain"
    assert "already uncertain" in (await actions.approve(aid))


def test_restart_quarantines_inflight_action(actions, conn):
    aid = actions.create("gmail_send", {"to": "a@b.c"}, "preview")
    conn.execute(
        "UPDATE pending_actions SET status = 'executing' WHERE id = ?", (aid,)
    )
    conn.commit()

    assert actions.recover_inflight() == 1
    assert conn.execute(
        "SELECT status FROM pending_actions WHERE id = ?", (aid,)
    ).fetchone()[0] == "uncertain"


async def test_edit_invalidates_old_send_button(actions, conn):
    sent: list[dict] = []

    async def executor(payload: dict) -> str:
        sent.append(payload)
        return "sent"

    actions.register_executor("gmail_send", executor)
    aid = actions.create("gmail_send", {"to": "a@b.c"}, "old draft")

    assert actions.request_edit(aid) is True
    assert "already editing" in (await actions.approve(aid))
    assert sent == []


async def test_draft_tool_creates_pending_action_and_notifies(conn, actions):
    """gmail_draft_reply never sends — it parks a pending action + pushes the
    approval prompt."""
    registry = ToolRegistry()
    notifications: list[tuple[int, str]] = []

    async def notify(action_id: int, preview: str) -> None:
        notifications.append((action_id, preview))

    gmail_tools.register(registry, gmail=None, actions=actions, notify_draft=notify)

    content, is_error = await registry.execute(
        "gmail_draft_reply",
        {"to": "sarah@example.com", "subject": "Re: Q3", "body": "Sounds good."},
    )
    assert not is_error
    assert "NOT sent" in content
    assert len(notifications) == 1
    row = conn.execute("SELECT * FROM pending_actions").fetchone()
    assert row["status"] == "pending"
    assert row["kind"] == "gmail_send"
    assert "sarah@example.com" in row["preview"]
