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
        payload_json = json.dumps(payload, sort_keys=True)
        existing = self.conn.execute(
            "SELECT id FROM pending_actions WHERE kind = ? AND payload = ? "
            "AND status IN ('pending','executing','uncertain') "
            "ORDER BY id DESC LIMIT 1",
            (kind, payload_json),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        aid = self.conn.execute(
            "INSERT INTO pending_actions(kind, payload, preview) VALUES (?, ?, ?)",
            (kind, payload_json, preview),
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
        # Claim before executing so a concurrent tap can't double-send.  The
        # action is not labelled sent until its provider confirms success.
        claimed = self.conn.execute(
            "UPDATE pending_actions SET status = 'executing' "
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
            # A timeout can happen after the provider accepted the request.  Do
            # not invite a blind retry that could duplicate an email/invite.
            self.conn.execute(
                "UPDATE pending_actions SET status = 'uncertain', "
                "resolved_at = datetime('now') WHERE id = ?", (action_id,)
            )
            self.conn.commit()
            return (
                f"I couldn't confirm action #{action_id}: {exc}. It may have "
                "gone through, so I won't retry it blindly—check the provider first."
            )
        self.conn.execute(
            "UPDATE pending_actions SET status = 'sent', "
            "resolved_at = datetime('now') WHERE id = ? AND status = 'executing'",
            (action_id,),
        )
        self.conn.commit()
        return outcome

    def recover_inflight(self) -> int:
        """A crash mid-send is ambiguous; quarantine it instead of re-sending."""
        cur = self.conn.execute(
            "UPDATE pending_actions SET status = 'uncertain', "
            "resolved_at = datetime('now') WHERE status = 'executing'"
        )
        self.conn.commit()
        if cur.rowcount:
            log.warning("recovered %d ambiguous outbound action(s)", cur.rowcount)
        return cur.rowcount

    def discard(self, action_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE pending_actions SET status = 'discarded', resolved_at = datetime('now') "
            "WHERE id = ? AND status IN ('pending','uncertain')",
            (action_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def request_edit(self, action_id: int) -> bool:
        """Invalidate the old Send button before asking for a redraft."""
        cur = self.conn.execute(
            "UPDATE pending_actions SET status = 'editing', "
            "resolved_at = datetime('now') WHERE id = ? AND status = 'pending'",
            (action_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0
