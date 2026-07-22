"""Calendar autonomy boundary: private changes act, shared changes draft."""

from __future__ import annotations

import json

from samantha.actions import PendingActions
from samantha.tools import ToolRegistry
from samantha.tools.calendar_tools import register


class FakeCalendar:
    def __init__(self, attendees: list[str] | None = None) -> None:
        self.attendees = attendees or []
        self.updates: list[tuple[str, dict]] = []
        self.creates: list[tuple] = []

    def get_event(self, event_id: str) -> dict:
        return {
            "id": event_id,
            "summary": "Weekly review",
            "start": "2026-07-23T10:00:00+08:00",
            "end": "2026-07-23T11:00:00+08:00",
            "location": "Boardroom",
            "attendees": self.attendees,
        }

    def update_event(self, event_id: str, **patch) -> dict:
        self.updates.append((event_id, patch))
        return {"id": event_id, "summary": "Weekly review"}

    def create_event(self, *args) -> dict:
        self.creates.append(args)
        return {"id": "event-new"}


def make_registry(conn, attendees=None):
    calendar = FakeCalendar(attendees)
    actions = PendingActions(conn)
    drafts: list[tuple[int, str]] = []

    async def notify(action_id: int, preview: str) -> None:
        drafts.append((action_id, preview))

    registry = ToolRegistry()
    register(registry, calendar, "Asia/Kuala_Lumpur", actions, notify)
    return registry, calendar, drafts


async def test_private_calendar_update_executes_immediately(conn):
    registry, calendar, drafts = make_registry(conn, attendees=[])

    receipt, is_error = await registry.execute(
        "calendar_update_event", {"event_id": "e1", "summary": "New title"}
    )

    assert is_error is False
    assert receipt == "Updated event e1."
    assert calendar.updates == [
        ("e1", {"send_updates": False, "summary": "New title"})
    ]
    assert drafts == []
    assert conn.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0


async def test_shared_calendar_update_waits_for_telegram_approval(conn):
    registry, calendar, drafts = make_registry(conn, attendees=["guest@example.com"])

    receipt, is_error = await registry.execute(
        "calendar_update_event",
        {"event_id": "e1", "start": "2026-07-23T10:00:00"},
    )

    assert is_error is False
    assert "ready for approval" in receipt
    assert "has not been changed" in receipt
    assert calendar.updates == []
    row = conn.execute("SELECT * FROM pending_actions").fetchone()
    assert row["kind"] == "calendar_update_with_attendees"
    assert json.loads(row["payload"])["event_id"] == "e1"
    assert drafts == [(row["id"], row["preview"])]


async def test_calendar_invite_waits_for_approval_but_private_event_does_not(conn):
    registry, calendar, drafts = make_registry(conn)
    common = {
        "summary": "Planning",
        "start": "2026-07-23T10:00:00",
        "end": "2026-07-23T11:00:00",
    }

    private_receipt, private_error = await registry.execute(
        "calendar_create_event", common
    )
    invite_receipt, invite_error = await registry.execute(
        "calendar_create_event",
        {
            **common,
            "attendees": ["guest@example.com"],
            "location": "Boardroom 3",
            "description": "Bring the launch brief",
        },
    )

    assert private_error is False and "Created event" in private_receipt
    assert len(calendar.creates) == 1
    assert invite_error is False and "not been created or sent" in invite_receipt
    assert len(drafts) == 1
    row = conn.execute("SELECT kind, preview FROM pending_actions").fetchone()
    assert row["kind"] == "calendar_create_with_attendees"
    assert "Location: Boardroom 3" in row["preview"]
    assert "Description: Bring the launch brief" in row["preview"]


async def test_calendar_delete_preview_is_informed_not_an_opaque_id(conn):
    registry, _calendar, drafts = make_registry(
        conn, attendees=["guest@example.com"]
    )

    receipt, is_error = await registry.execute(
        "calendar_delete_event", {"event_id": "e1"}
    )

    assert is_error is False
    assert "waiting for approval" in receipt
    preview = drafts[0][1]
    assert "Weekly review" in preview
    assert "2026-07-23T10:00:00" in preview
    assert "guest@example.com" in preview
    assert "Event id: e1" in preview
