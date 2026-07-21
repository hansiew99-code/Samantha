"""ClickUp: polled task sync into the local tasks table + due-soon events.

Task mutations on the owner's own tasks are auto-allowed (BRIEF §8) — they
don't reach other people.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from ..db import kv_get, kv_set
from ..events import EventBus

log = logging.getLogger(__name__)

BASE = "https://api.clickup.com/api/v2"
DUE_SOON = timedelta(hours=24)


class ClickUpClient:
    def __init__(self, token: str, team_id: str) -> None:
        self.token = token
        self.team_id = team_id

    def _headers(self) -> dict:
        return {"Authorization": self.token}

    async def fetch_open_tasks(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BASE}/team/{self.team_id}/task",
                headers=self._headers(),
                params={"include_closed": "false", "subtasks": "true"},
            )
            resp.raise_for_status()
        out = []
        for t in resp.json().get("tasks", []):
            due_ms = t.get("due_date")
            out.append(
                {
                    "id": t["id"],
                    "name": t.get("name", "(untitled)"),
                    "status": (t.get("status") or {}).get("status", "open"),
                    "due_at": (
                        datetime.fromtimestamp(int(due_ms) / 1000, tz=timezone.utc).isoformat()
                        if due_ms
                        else None
                    ),
                    "list": (t.get("list") or {}).get("name", ""),
                    "url": t.get("url", ""),
                }
            )
        return out

    async def set_status(self, task_id: str, status: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(
                f"{BASE}/task/{task_id}",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"status": status},
            )
            resp.raise_for_status()


class ClickUpSync:
    """Poller: mirrors open ClickUp tasks locally and raises due-soon events
    (each task alerts once — tracked in integration_state)."""

    def __init__(self, client: ClickUpClient, conn: sqlite3.Connection, bus: EventBus) -> None:
        self.client = client
        self.conn = conn
        self.bus = bus

    async def poll(self) -> None:
        try:
            tasks = await self.client.fetch_open_tasks()
        except Exception:
            log.exception("clickup poll failed")
            return

        open_ids = set()
        for t in tasks:
            open_ids.add(t["id"])
            self.conn.execute(
                """INSERT INTO tasks(source, external_id, title, due_at, status, data, updated_at)
                   VALUES ('clickup', ?, ?, ?, 'open', ?, datetime('now'))
                   ON CONFLICT(source, external_id) DO UPDATE SET
                     title = excluded.title, due_at = excluded.due_at,
                     status = 'open', data = excluded.data, updated_at = datetime('now')""",
                (t["id"], t["name"], t["due_at"], json.dumps(t)),
            )
        # Anything we mirrored that ClickUp no longer lists as open is done.
        rows = self.conn.execute(
            "SELECT external_id FROM tasks WHERE source = 'clickup' AND status = 'open'"
        ).fetchall()
        for row in rows:
            if row["external_id"] not in open_ids:
                self.conn.execute(
                    "UPDATE tasks SET status = 'done', updated_at = datetime('now') "
                    "WHERE source = 'clickup' AND external_id = ?",
                    (row["external_id"],),
                )
        self.conn.commit()

        now = datetime.now(timezone.utc)
        for t in tasks:
            if not t["due_at"]:
                continue
            due = datetime.fromisoformat(t["due_at"])
            key = f"clickup.due_notified.{t['id']}"
            if now <= due <= now + DUE_SOON and kv_get(self.conn, key) is None:
                kv_set(self.conn, key, now.isoformat())
                self.bus.enqueue(
                    "clickup", "due_soon", t["list"] or "*",
                    {"title": t["name"], "due_at": t["due_at"], "url": t["url"], "id": t["id"]},
                )
