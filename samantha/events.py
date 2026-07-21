"""Event bus + batched sweeps (BRIEF §6).

Flow: integration pollers enqueue rows → the rules filter drops suppressed
events deterministically (zero tokens) → survivors are judged in ONE Haiku
call per sweep (never one call per event) → notify now / fold into next
digest / ignore.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, time
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .brain import Brain
from .config import Settings
from .context import assemble_base
from .memory import Memory
from .router import HAIKU
from .rules import RulesEngine

log = logging.getLogger(__name__)

MAX_EVENTS_PER_SWEEP = 20

Notify = Callable[[str], Awaitable[None]]

SWEEP_INSTRUCTIONS = """\
You are doing a background triage sweep — the owner has NOT messaged you; you \
are deciding which of the queued events below deserve their attention right \
now. Be protective of their focus: 'notify' only for things that are urgent, \
time-sensitive today, or from someone marked important. Everything routine \
goes to 'digest' (it will appear in the next morning/evening summary). Noise \
goes to 'ignore'.

Reply with ONLY a JSON object, no prose:
{"decisions": [{"i": <event index>, "action": "notify"|"digest"|"ignore", \
"message": "<only for notify: the exact short message to push to the owner>"}]}
Every event index must appear exactly once."""


def in_quiet_hours(now: datetime, quiet: tuple[time, time]) -> bool:
    start, end = quiet
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight


class EventBus:
    def __init__(self, conn: sqlite3.Connection, rules: RulesEngine) -> None:
        self.conn = conn
        self.rules = rules

    def enqueue(self, source: str, kind: str, scope: str, payload: dict) -> int | None:
        """Rules are checked at enqueue time too — a suppressed source never
        even lands in the queue."""
        if not self.rules.allows(source, scope):
            log.debug("suppressed at enqueue: %s/%s %s", source, kind, scope)
            return None
        eid = self.conn.execute(
            "INSERT INTO events_queue(source, kind, scope, payload) VALUES (?, ?, ?, ?)",
            (source, kind, scope, json.dumps(payload)),
        ).lastrowid
        self.conn.commit()
        return eid

    def pending(self, limit: int = MAX_EVENTS_PER_SWEEP) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events_queue WHERE processed_at IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def mark(self, event_id: int, disposition: str) -> None:
        self.conn.execute(
            "UPDATE events_queue SET processed_at = datetime('now'), disposition = ? "
            "WHERE id = ?",
            (disposition, event_id),
        )
        self.conn.commit()

    def digest_backlog(self, hours: int = 26) -> list[sqlite3.Row]:
        """Events held for the next digest (plus anything still unprocessed)."""
        return self.conn.execute(
            "SELECT * FROM events_queue WHERE "
            "(disposition = 'digest' AND processed_at >= datetime('now', ?)) "
            "OR processed_at IS NULL ORDER BY id",
            (f"-{hours} hours",),
        ).fetchall()


class Sweeper:
    """One Haiku call per sweep, covering every pending event."""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        memory: Memory,
        brain: Brain | None,
        notify: Notify,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.memory = memory
        self.brain = brain
        self.notify = notify

    async def run_sweep(self) -> int:
        """Returns the number of events handled. Zero LLM calls when the queue
        is empty, suppressed-only, or we're inside quiet hours."""
        now = datetime.now(ZoneInfo(self.settings.timezone))
        if in_quiet_hours(now, self.settings.quiet_hours):
            return 0

        events = self.bus.pending()
        if not events:
            return 0

        # Deterministic re-filter (a rule may have been added after enqueue).
        survivors: list[sqlite3.Row] = []
        for ev in events:
            if self.bus.rules.allows(ev["source"], ev["scope"]):
                survivors.append(ev)
            else:
                self.bus.mark(ev["id"], "suppressed")
        if not survivors:
            return len(events)
        if self.brain is None:
            return 0  # no LLM available; leave for the digest

        decisions = await self._judge(survivors)
        for ev in survivors:
            decision = decisions.get(ev["id"], {"action": "digest"})
            action = decision.get("action", "digest")
            if action == "notify":
                vip = self.bus.rules.is_vip(ev["source"], ev["scope"])
                prefix = "❗ " if vip else ""
                await self.notify(prefix + (decision.get("message") or self._fallback_line(ev)))
                self.bus.mark(ev["id"], "notified")
            elif action == "ignore":
                self.bus.mark(ev["id"], "ignored")
            else:
                self.bus.mark(ev["id"], "digest")
        return len(events)

    async def _judge(self, events: list[sqlite3.Row]) -> dict[int, dict]:
        listing = []
        index_to_id: dict[int, int] = {}
        for i, ev in enumerate(events):
            index_to_id[i] = ev["id"]
            payload = json.loads(ev["payload"])
            snippet = str(payload.get("snippet") or payload.get("text") or payload.get("title") or "")[:200]
            listing.append(
                {"i": i, "source": ev["source"], "kind": ev["kind"],
                 "from": ev["scope"], "snippet": snippet}
            )
        system = assemble_base(self.memory)
        system.append({"type": "text", "text": SWEEP_INSTRUCTIONS})
        user = json.dumps({"events": listing}, ensure_ascii=False)
        raw = await self.brain.run_loop(
            HAIKU, system, [{"role": "user", "content": user}], purpose="sweep", tools=[]
        )
        decisions: dict[int, dict] = {}
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            parsed = json.loads(match.group(0)) if match else {}
            for d in parsed.get("decisions", []):
                if int(d.get("i", -1)) in index_to_id:
                    decisions[index_to_id[int(d["i"])]] = d
        except (json.JSONDecodeError, ValueError, AttributeError):
            log.warning("sweep: unparseable decisions, defaulting to digest: %r", raw[:200])
        return decisions

    @staticmethod
    def _fallback_line(ev: sqlite3.Row) -> str:
        payload = json.loads(ev["payload"])
        return f"[{ev['source']}] {ev['kind']}: {payload.get('subject') or payload.get('title') or payload.get('text', '')}"[:200]
