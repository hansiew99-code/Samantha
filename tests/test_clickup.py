"""ClickUp sync records freshness instead of silently serving stale tasks."""

from __future__ import annotations

import pytest

from samantha.db import sync_status
from samantha.events import EventBus
from samantha.integrations.clickup import ClickUpClient, ClickUpSync
from samantha.rules import RulesEngine
from samantha.tools import ToolRegistry
from samantha.tools.clickup_tools import register as register_clickup_tools


class FakeClient:
    def __init__(self, tasks=None, error: Exception | None = None) -> None:
        self.tasks = tasks or []
        self.error = error

    async def fetch_open_tasks(self):
        if self.error:
            raise self.error
        return self.tasks


async def test_clickup_success_records_freshness_and_tasks(conn, memory):
    client = FakeClient(
        [
            {
                "id": "t1",
                "name": "Ship report",
                "status": "open",
                "due_at": None,
                "list": "Work",
                "url": "https://example.invalid/t1",
            }
        ]
    )
    sync = ClickUpSync(client, conn, EventBus(conn, RulesEngine(conn, memory)))

    assert await sync.poll() is True

    assert sync_status(conn, "clickup")["last_success"] is not None
    assert sync_status(conn, "clickup")["last_error"] is None
    assert conn.execute("SELECT title FROM tasks").fetchone()["title"] == "Ship report"


async def test_clickup_failure_preserves_tasks_and_records_staleness(conn, memory):
    conn.execute(
        "INSERT INTO tasks(source, external_id, title) VALUES ('clickup', 'old', 'Existing')"
    )
    conn.commit()
    sync = ClickUpSync(
        FakeClient(error=TimeoutError("offline")),
        conn,
        EventBus(conn, RulesEngine(conn, memory)),
    )

    assert await sync.poll() is False

    assert sync_status(conn, "clickup")["last_error"] == "TimeoutError"
    assert conn.execute("SELECT title FROM tasks").fetchone()["title"] == "Existing"


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeHttpClient:
    def __init__(self, pages: dict[int, dict]) -> None:
        self.pages = pages
        self.requested_pages: list[int] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url, *, headers, params):
        assert headers["Authorization"] == "token"
        page = params["page"]
        self.requested_pages.append(page)
        return FakeResponse(self.pages[page])


def clickup_task(task_id: str, name: str) -> dict:
    return {
        "id": task_id,
        "name": name,
        "status": {"status": "open"},
        "due_date": None,
        "list": {"name": "Work"},
        "url": f"https://example.invalid/{task_id}",
    }


async def test_paginated_fetch_keeps_later_page_task_open(
    conn, memory, monkeypatch
):
    http = FakeHttpClient(
        {
            0: {"tasks": [clickup_task("t1", "First")], "last_page": False},
            1: {"tasks": [clickup_task("t2", "Second")], "last_page": True},
        }
    )
    monkeypatch.setattr(
        "samantha.integrations.clickup.httpx.AsyncClient",
        lambda **_kwargs: http,
    )
    conn.execute(
        "INSERT INTO tasks(source, external_id, title, status) "
        "VALUES ('clickup', 't2', 'Existing second', 'open')"
    )
    conn.commit()
    sync = ClickUpSync(
        ClickUpClient("token", "team"),
        conn,
        EventBus(conn, RulesEngine(conn, memory)),
    )

    assert await sync.poll() is True

    assert http.requested_pages == [0, 1]
    rows = conn.execute(
        "SELECT external_id, status FROM tasks ORDER BY external_id"
    ).fetchall()
    assert [(row["external_id"], row["status"]) for row in rows] == [
        ("t1", "open"),
        ("t2", "open"),
    ]


async def test_repeated_clickup_page_fails_instead_of_looping(monkeypatch):
    repeated = {"tasks": [clickup_task("t1", "First")], "last_page": False}

    class RepeatingHttp(FakeHttpClient):
        async def get(self, _url, *, headers, params):
            self.requested_pages.append(params["page"])
            return FakeResponse(repeated)

    http = RepeatingHttp({})
    monkeypatch.setattr(
        "samantha.integrations.clickup.httpx.AsyncClient",
        lambda **_kwargs: http,
    )

    with pytest.raises(RuntimeError, match="did not advance"):
        await ClickUpClient("token", "team").fetch_open_tasks()

    assert http.requested_pages == [0, 1]


async def test_live_tool_read_warns_when_refresh_failed(conn, memory):
    conn.execute(
        "INSERT INTO tasks(source, external_id, title) "
        "VALUES ('clickup', 'old', 'Cached task')"
    )
    conn.commit()
    client = FakeClient(error=TimeoutError("offline"))
    sync = ClickUpSync(client, conn, EventBus(conn, RulesEngine(conn, memory)))
    registry = ToolRegistry()
    register_clickup_tools(registry, conn, client, sync)

    result, is_error = await registry.execute(
        "clickup_list_tasks", {"refresh": True}
    )

    assert is_error is False
    assert "live refresh failed" in result
    assert "Cached task" in result
