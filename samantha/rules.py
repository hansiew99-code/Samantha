"""Persisted behavior rules (BRIEF §7): "stop reminding me about ClickUp"
becomes a row that the event filter enforces deterministically, forever,
at zero token cost. Rules are deactivated, never deleted.
"""

from __future__ import annotations

import sqlite3

from .memory import Memory

SUPPRESS = "suppress"
VIP = "vip"

KNOWN_ACTIONS = (SUPPRESS, VIP)


class RulesEngine:
    def __init__(self, conn: sqlite3.Connection, memory: Memory) -> None:
        self.conn = conn
        self.memory = memory

    def add(self, source: str, action: str, scope: str = "*", detail: str | None = None) -> int:
        if action not in KNOWN_ACTIONS:
            raise ValueError(f"unknown rule action {action!r}; use one of {KNOWN_ACTIONS}")
        rid = self.conn.execute(
            "INSERT INTO rules(source, action, scope, detail) VALUES (?, ?, ?, ?)",
            (source.lower(), action, scope.lower(), detail),
        ).lastrowid
        assert rid is not None
        self.conn.commit()
        self._sync_core_memory()
        return rid

    def deactivate(self, rule_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE rules SET active = 0 WHERE id = ? AND active = 1", (rule_id,)
        )
        self.conn.commit()
        self._sync_core_memory()
        return cur.rowcount > 0

    def active_rules(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rules WHERE active = 1 ORDER BY id"
        ).fetchall()

    # -- the deterministic filter (zero tokens) ------------------------------

    def allows(self, source: str, scope: str) -> bool:
        """False if any active suppress rule matches this event's source+scope.
        A suppressed event is dropped before any LLM ever sees it."""
        source = source.lower()
        scope = (scope or "*").lower()
        for rule in self.active_rules():
            if rule["action"] != SUPPRESS:
                continue
            if rule["source"] not in (source, "*"):
                continue
            if rule["scope"] == "*" or rule["scope"] in scope:
                return False
        return True

    def is_vip(self, source: str, scope: str) -> bool:
        source = source.lower()
        scope = (scope or "").lower()
        for rule in self.active_rules():
            if rule["action"] != VIP:
                continue
            if rule["source"] in (source, "*") and (
                rule["scope"] == "*" or rule["scope"] in scope
            ):
                return True
        return False

    # -- keep her aware of her own standing orders (BRIEF §7) ----------------

    def _sync_core_memory(self) -> None:
        rules = self.active_rules()
        if not rules:
            self.memory.set_core("standing_rules", "(none)")
            return
        lines = [
            f"- #{r['id']}: {r['action']} {r['source']}"
            + (f" (scope: {r['scope']})" if r["scope"] != "*" else " (everything)")
            + (f" — {r['detail']}" if r["detail"] else "")
            for r in rules
        ]
        self.memory.set_core("standing_rules", "\n".join(lines))
