"""Pending outbound actions (BRIEF §8): anything that reaches another person is
drafted here and executed only after a Telegram approval tap. Approval is
idempotent — a double-tap can never send twice.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

Executor = Callable[[dict], Awaitable[str]]  # payload -> human-readable outcome


class PendingActions:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._executors: dict[str, Executor] = {}

    def register_executor(self, kind: str, executor: Executor) -> None:
        self._executors[kind] = executor

    def create(self, kind: str, payload: dict, preview: str) -> int:
        aid = self.conn.execute(
            "INSERT INTO pending_actions(kind, payload, preview) VALUES (?, ?, ?)",
            (kind, json.dumps(payload), preview),
        ).lastrowid
        assert aid is not None
        self.conn.commit()
        return aid

    def get(self, action_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
        ).fetchone()

    async def approve(self, action_id: int) -> str:
        row = self.get(action_id)
        if row is None:
            return f"No pending action #{action_id}."
        if row["status"] != "pending":
            return f"Action #{action_id} was already {row['status']} — not re-sending."
        executor = self._executors.get(row["kind"])
        if executor is None:
            return f"No executor for {row['kind']} — is that integration configured?"
        # Claim before executing so a concurrent tap can't double-send.
        claimed = self.conn.execute(
            "UPDATE pending_actions SET status = 'sent', resolved_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (action_id,),
        )
        self.conn.commit()
        if claimed.rowcount == 0:
            return f"Action #{action_id} was already handled."
        try:
            outcome = await executor(json.loads(row["payload"]))
        except Exception as exc:  # noqa: BLE001
            log.exception("action %d failed", action_id)
            self.conn.execute(
                "UPDATE pending_actions SET status = 'failed' WHERE id = ?", (action_id,)
            )
            self.conn.commit()
            return f"Sending failed: {exc}"
        return outcome

    def discard(self, action_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE pending_actions SET status = 'discarded', resolved_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (action_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0
