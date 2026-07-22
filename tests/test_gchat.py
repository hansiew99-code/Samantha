"""Google Chat: pure parsing/selection + the poll cursor (prime, advance,
self/bot filtering) — all offline."""

from __future__ import annotations

from samantha.db import kv_get
from samantha.integrations.gchat import (
    CURSOR_KEY,
    GChatClient,
    newest_create_time,
    parse_message,
    select_new,
)


def raw(mid: str, sender: str, text: str, t: str, dn: str = "", stype: str = "HUMAN") -> dict:
    return {
        "name": f"spaces/S/messages/{mid}",
        "sender": {"name": sender, "displayName": dn, "type": stype},
        "text": text,
        "createTime": t,
    }


class FakeGChat(GChatClient):
    """Stands in for the API by returning canned parsed messages, emulating the
    server-side `createTime >` filter."""

    def __init__(self, messages: list[dict], self_id: str | None = None) -> None:
        super().__init__(creds=object(), self_id=self_id)
        self._messages = list(messages)

    def recent_messages(self, since_iso: str | None = None, per_space: int = 10) -> list[dict]:
        msgs = self._messages
        if since_iso is not None:
            msgs = [m for m in msgs if m.get("create_time", "") > since_iso]
        return sorted(msgs, key=lambda m: m.get("create_time", ""), reverse=True)


def test_parse_message_flattens():
    m = parse_message(raw("m1", "users/2", "hi", "2026-07-22T10:00:00Z", dn="Sarah"), "spaces/S")
    assert m["sender"] == "users/2"
    assert m["sender_name"] == "Sarah"
    assert m["text"] == "hi"
    assert m["space"] == "spaces/S"
    assert m["create_time"] == "2026-07-22T10:00:00Z"


def test_select_new_filters_self_bot_stale_and_empty():
    msgs = [
        parse_message(raw("a", "users/me", "mine", "2026-07-22T11:00:00Z"), "s"),
        parse_message(raw("b", "users/2", "from sarah", "2026-07-22T11:05:00Z", dn="Sarah"), "s"),
        parse_message(raw("c", "users/bot", "auto ping", "2026-07-22T11:06:00Z", stype="BOT"), "s"),
        parse_message(raw("d", "users/2", "already seen", "2026-07-22T09:00:00Z"), "s"),
        parse_message(raw("e", "users/2", "", "2026-07-22T11:10:00Z"), "s"),
    ]
    new = select_new(msgs, since="2026-07-22T10:00:00Z", self_id="users/me")
    assert [m["text"] for m in new] == ["from sarah"]


def test_newest_create_time():
    msgs = [
        parse_message(raw("a", "users/2", "x", "2026-07-22T10:00:00Z"), "s"),
        parse_message(raw("b", "users/2", "y", "2026-07-22T12:00:00Z"), "s"),
    ]
    assert newest_create_time(msgs, None) == "2026-07-22T12:00:00Z"
    assert newest_create_time([], "fallback") == "fallback"


def test_poll_primes_then_returns_only_new_inbound(conn):
    messages = [parse_message(raw("a", "users/2", "early", "2026-07-22T09:00:00Z", dn="Sarah"), "s")]
    gchat = FakeGChat(messages, self_id="users/me")

    # First poll primes the cursor to the newest message and backfills nothing.
    assert gchat.poll_new(conn) == []
    assert kv_get(conn, CURSOR_KEY) == "2026-07-22T09:00:00Z"

    # A newer message from Sarah arrives, plus one the owner sent themselves.
    gchat._messages.append(parse_message(raw("b", "users/2", "you free?", "2026-07-22T10:00:00Z", dn="Sarah"), "s"))
    gchat._messages.append(parse_message(raw("c", "users/me", "my own reply", "2026-07-22T10:05:00Z"), "s"))

    new = gchat.poll_new(conn)
    assert [m["text"] for m in new] == ["you free?"]  # own message filtered out
    # Cursor advances past everything seen (own message included) — no re-scan.
    assert kv_get(conn, CURSOR_KEY) == "2026-07-22T10:05:00Z"

    # Nothing newer → nothing surfaced.
    assert gchat.poll_new(conn) == []
