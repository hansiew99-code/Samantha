"""Google Chat (v1): poll the spaces the owner is in for messages other people
send them, and expose a read tool for the brain.

Rides on the same Google OAuth as Calendar/Gmail with two extra read-only
scopes (chat.spaces.readonly + chat.messages.readonly). With *user*
credentials the API only ever sees spaces the owner already belongs to — their
DMs and rooms — so there's no bot to install and nothing admin-level. The
integration stays off until GCHAT_ENABLED=1.

Parsing and new-message selection are pure functions (unit-tested, zero
network); the client is a thin I/O shell around them, exactly like gmail.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..db import kv_get, kv_set

log = logging.getLogger(__name__)

CURSOR_KEY = "gchat.last_seen"  # RFC3339 createTime of the newest message handled
TEXT_CLIP = 500


# -- pure helpers (no API) ----------------------------------------------------


def parse_message(raw: dict, space_name: str) -> dict:
    """Flatten a Chat message resource into the shape the bus/tools use."""
    sender = raw.get("sender", {}) or {}
    return {
        "name": raw.get("name", ""),  # spaces/AAA/messages/BBB
        "space": space_name,
        "sender": sender.get("name", ""),  # users/123456
        "sender_name": sender.get("displayName", ""),
        "sender_type": sender.get("type", ""),  # HUMAN | BOT
        "text": (raw.get("text") or "")[:TEXT_CLIP],
        "create_time": raw.get("createTime", ""),
    }


def select_new(messages: list[dict], *, since: str | None, self_id: str | None) -> list[dict]:
    """Parsed messages that are genuinely new inbound text: strictly newer than
    `since`, carry text, weren't sent by the owner, and aren't from a bot.

    createTime is RFC3339 Zulu, so plain string comparison orders it correctly."""
    out: list[dict] = []
    for m in messages:
        if not m.get("text"):
            continue
        if m.get("sender_type") == "BOT":
            continue
        if self_id and m.get("sender") == self_id:
            continue
        if since is not None and m.get("create_time", "") <= since:
            continue
        out.append(m)
    return out


def newest_create_time(messages: list[dict], fallback: str | None) -> str | None:
    """The high-water mark to store as the next cursor — computed over every
    fetched message (self/bot included) so we never re-scan the same window."""
    times = [m.get("create_time", "") for m in messages if m.get("create_time")]
    return max(times) if times else fallback


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# -- API client (blocking; call via asyncio.to_thread) ------------------------


@dataclass
class GChatClient:
    creds: object
    self_id: str | None = None
    _svc: object | None = None  # injected in tests; None => build a real service

    def _service(self):
        if self._svc is not None:
            return self._svc
        from googleapiclient.discovery import build

        return build("chat", "v1", credentials=self.creds, cache_discovery=False)

    def list_spaces(self, max_results: int = 50) -> list[dict]:
        resp = self._service().spaces().list(pageSize=max_results).execute()
        return resp.get("spaces", [])

    def _space_messages(self, space_name: str, since_iso: str | None, per_space: int) -> list[dict]:
        params: dict = dict(parent=space_name, pageSize=per_space, orderBy="createTime desc")
        if since_iso:
            params["filter"] = f'createTime > "{since_iso}"'
        resp = self._service().spaces().messages().list(**params).execute()
        return [parse_message(m, space_name) for m in resp.get("messages", [])]

    def recent_messages(self, since_iso: str | None = None, per_space: int = 10) -> list[dict]:
        """Parsed messages across every space the owner is in (newest first),
        optionally only those after `since_iso`. One failing space never sinks
        the whole poll."""
        out: list[dict] = []
        for space in self.list_spaces():
            name = space.get("name")
            if not name:
                continue
            try:
                out.extend(self._space_messages(name, since_iso, per_space))
            except Exception:
                log.debug("gchat: listing messages for %s failed", name, exc_info=True)
        return out

    def recent_inbound(self, since_iso: str, per_space: int = 5) -> list[dict]:
        """Recent messages from other people (own/bot/empty filtered out) — what
        a proactive brief should read on its own, without being asked."""
        return select_new(self.recent_messages(since_iso, per_space), since=None, self_id=self.self_id)

    def poll_new(self, conn) -> list[dict]:
        """New inbound messages since the stored cursor. First run primes the
        cursor to 'now' and returns nothing (no backfill flood), matching the
        gmail poller's contract."""
        cursor = kv_get(conn, CURSOR_KEY)
        if cursor is None:
            primed = newest_create_time(self.recent_messages(None, per_space=1), None)
            kv_set(conn, CURSOR_KEY, primed or _now_rfc3339())
            return []
        fetched = self.recent_messages(cursor, per_space=25)
        new = select_new(fetched, since=cursor, self_id=self.self_id)
        newest = newest_create_time(fetched, cursor)
        if newest and newest != cursor:
            kv_set(conn, CURSOR_KEY, newest)
        return new
