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
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..db import DB_WRITE_LOCK, kv_get, kv_set, stage_source_event

log = logging.getLogger(__name__)

CURSOR_KEY = "gchat.last_seen"  # RFC3339 createTime of the newest message handled
SPACE_CURSOR_PREFIX = "gchat.space_cursor."
PER_SPACE_MIGRATED_KEY = "gchat.per_space_cursors"
TEXT_CLIP = 500
RECENT_INBOUND_LIMIT = 25


class PartialGoogleChatReadError(RuntimeError):
    """At least one Chat space could not be read during a best-effort poll."""


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


def _parse_rfc3339(value: str) -> datetime | None:
    """Parse Google RFC3339 timestamps independent of fractional precision."""
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _message_time(message: dict) -> datetime:
    return _parse_rfc3339(str(message.get("create_time", ""))) or datetime.min.replace(
        tzinfo=timezone.utc
    )


def select_new(messages: list[dict], *, since: str | None, self_id: str | None) -> list[dict]:
    """Parsed messages that are genuinely new inbound text: strictly newer than
    `since`, carry text, weren't sent by the owner, and aren't from a bot.

    RFC3339 allows variable fractional-second precision, so comparisons use
    parsed instants rather than their wire-format strings."""
    cutoff = _parse_rfc3339(since) if since is not None else None
    if since is not None and cutoff is None:
        raise ValueError(f"invalid Google Chat cursor: {since!r}")
    out: list[dict] = []
    for m in messages:
        if not m.get("text"):
            continue
        if m.get("sender_type") == "BOT":
            continue
        if self_id and m.get("sender") == self_id:
            continue
        created = _parse_rfc3339(str(m.get("create_time", "")))
        if created is None:
            continue
        if cutoff is not None and created <= cutoff:
            continue
        out.append(m)
    return out


def newest_create_time(messages: list[dict], fallback: str | None) -> str | None:
    """The high-water mark to store as the next cursor — computed over every
    fetched message (self/bot included) so we never re-scan the same window."""
    valid = [m for m in messages if _parse_rfc3339(str(m.get("create_time", "")))]
    return str(max(valid, key=_message_time)["create_time"]) if valid else fallback


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# -- API client (blocking; call via asyncio.to_thread) ------------------------


@dataclass
class GChatClient:
    creds: object
    self_id: str | None = None
    _svc: object | None = None  # injected in tests; None => build a real service
    last_read_complete: bool = field(default=True, init=False)

    def _service(self):
        if self._svc is not None:
            return self._svc
        from googleapiclient.discovery import build

        return build("chat", "v1", credentials=self.creds, cache_discovery=False)

    def list_spaces(self, max_results: int | None = None) -> list[dict]:
        out: list[dict] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while max_results is None or len(out) < max_results:
            page_size = 100 if max_results is None else min(100, max_results - len(out))
            params: dict = {"pageSize": page_size}
            if page_token:
                params["pageToken"] = page_token
            resp = self._service().spaces().list(**params).execute()
            out.extend(resp.get("spaces", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            if page_token in seen_tokens:
                raise RuntimeError("Google Chat spaces pagination token repeated")
            seen_tokens.add(page_token)
        return out if max_results is None else out[:max_results]

    def _space_messages(
        self, space_name: str, since_iso: str | None, per_space: int | None
    ) -> list[dict]:
        out: list[dict] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while per_space is None or len(out) < per_space:
            page_size = 100 if per_space is None else min(100, per_space - len(out))
            params: dict = dict(
                parent=space_name,
                pageSize=page_size,
                orderBy="createTime desc",
            )
            if since_iso:
                params["filter"] = f'createTime > "{since_iso}"'
            if page_token:
                params["pageToken"] = page_token
            resp = self._service().spaces().messages().list(**params).execute()
            out.extend(parse_message(m, space_name) for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            if page_token in seen_tokens:
                raise RuntimeError("Google Chat messages pagination token repeated")
            seen_tokens.add(page_token)
        return out if per_space is None else out[:per_space]

    def recent_messages(self, since_iso: str | None = None, per_space: int = 10) -> list[dict]:
        """Parsed messages across every space the owner is in (newest first),
        optionally only those after `since_iso`. One failing space never sinks
        the whole poll."""
        out: list[dict] = []
        complete = True
        for space in self.list_spaces():
            name = space.get("name")
            if not name:
                continue
            try:
                out.extend(self._space_messages(name, since_iso, per_space))
            except Exception:
                complete = False
                log.debug("gchat: listing messages for %s failed", name, exc_info=True)
        self.last_read_complete = complete
        return sorted(out, key=_message_time, reverse=True)

    def recent_inbound(
        self,
        since_iso: str,
        per_space: int = 5,
        limit: int = RECENT_INBOUND_LIMIT,
    ) -> list[dict]:
        """Recent messages from other people (own/bot/empty filtered out) — what
        a proactive brief should read on its own, without being asked."""
        inbound = select_new(
            self.recent_messages(since_iso, per_space),
            since=None,
            self_id=self.self_id,
        )
        return sorted(inbound, key=_message_time, reverse=True)[:limit]

    def poll_new(self, conn) -> list[dict]:
        """New inbound messages with an independent cursor per Chat space.

        The old global high-water mark could advance on a healthy room while a
        second room was temporarily unreadable, permanently skipping messages
        in the failed room.  Per-space checkpoints let healthy rooms progress
        without sacrificing retry safety for the failed one.
        """
        legacy_cursor = kv_get(conn, CURSOR_KEY)
        migrated = kv_get(conn, PER_SPACE_MIGRATED_KEY) == "1"
        first_seen_at = _now_rfc3339()
        events: list[dict] = []
        complete = True
        cursor_values: list[str] = []
        state_updates: dict[str, str] = {}

        for space in self.list_spaces():
            name = space.get("name")
            if not name:
                continue
            key = f"{SPACE_CURSOR_PREFIX}{name}"
            cursor = kv_get(conn, key)
            try:
                if cursor is None and migrated:
                    # A newly joined room should not flood historical messages.
                    priming = self._space_messages(name, None, 1)
                    cursor = newest_create_time(priming, _now_rfc3339())
                    assert cursor is not None
                    state_updates[key] = cursor
                    cursor_values.append(cursor)
                    continue

                if cursor is None and legacy_cursor is None:
                    # First ever run: establish a baseline, no backfill.
                    priming = self._space_messages(name, None, 1)
                    cursor = newest_create_time(priming, _now_rfc3339())
                    assert cursor is not None
                    state_updates[key] = cursor
                    cursor_values.append(cursor)
                    continue

                cursor = cursor or legacy_cursor
                assert cursor is not None
                # Polling is a correctness path: page to exhaustion before
                # advancing this room's cursor. Read tools remain bounded.
                fetched = self._space_messages(name, cursor, None)
            except Exception:
                complete = False
                if cursor is None and legacy_cursor is None:
                    # Establish the earliest safe retry point even though this
                    # room is currently unreadable.  Once it recovers, messages
                    # that arrived during the outage are still eligible.
                    state_updates[key] = first_seen_at
                log.warning("gchat: poll failed for %s; keeping its cursor", name, exc_info=True)
                continue

            events.extend(select_new(fetched, since=cursor, self_id=self.self_id))
            newest = newest_create_time(fetched, cursor)
            assert newest is not None
            if newest != cursor:
                state_updates[key] = newest
            cursor_values.append(newest)

        self.last_read_complete = complete
        if complete:
            state_updates[PER_SPACE_MIGRATED_KEY] = "1"
            # Retain the legacy aggregate cursor for observability and smooth
            # upgrades from older deployments; correctness no longer uses it.
            newest_overall = newest_create_time(
                [{"create_time": value} for value in cursor_values],
                legacy_cursor or _now_rfc3339(),
            )
            assert newest_overall is not None
            state_updates[CURSOR_KEY] = newest_overall

        ordered = sorted(events, key=_message_time)
        with DB_WRITE_LOCK:
            try:
                for event in ordered:
                    message_name = str(event.get("name") or "")
                    if not message_name:
                        continue
                    stage_source_event(
                        conn,
                        dedupe_key=f"gchat:{message_name}",
                        source="gchat",
                        kind="new_message",
                        scope=str(event.get("sender_name") or event.get("sender") or "*"),
                        payload=event,
                    )
                for key, value in state_updates.items():
                    kv_set(conn, key, value, commit=False)
                conn.commit()
            except Exception:
                conn.rollback()
                self.last_read_complete = False
                raise
        return ordered
