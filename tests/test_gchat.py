"""Google Chat: pure parsing/selection + the poll cursor (prime, advance,
self/bot filtering) — all offline."""

from __future__ import annotations

from samantha.db import kv_get, kv_set
from samantha.integrations.gchat import (
    CURSOR_KEY,
    GChatClient,
    _parse_rfc3339,
    newest_create_time,
    parse_message,
    select_new,
)
from samantha.tools import ToolRegistry
from samantha.tools import gchat_tools


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

    def list_spaces(self, max_results: int | None = None) -> list[dict]:
        names = list(dict.fromkeys(m.get("space", "s") for m in self._messages))
        if max_results is not None:
            names = names[:max_results]
        return [{"name": name} for name in names]

    def _space_messages(
        self, space_name: str, since_iso: str | None, per_space: int | None
    ) -> list[dict]:
        msgs = [m for m in self._messages if m.get("space", "s") == space_name]
        if since_iso is not None:
            cutoff = _parse_rfc3339(since_iso)
            msgs = [
                m
                for m in msgs
                if _parse_rfc3339(m.get("create_time", ""))
                and _parse_rfc3339(m.get("create_time", "")) > cutoff
            ]
        msgs = sorted(
            msgs, key=lambda m: _parse_rfc3339(m.get("create_time", "")), reverse=True
        )
        return msgs if per_space is None else msgs[:per_space]


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


def test_rfc3339_comparisons_handle_variable_fractional_precision():
    whole = parse_message(raw("a", "users/2", "whole", "2026-07-22T10:00:00Z"), "s")
    fractional = parse_message(
        raw("b", "users/2", "fractional", "2026-07-22T10:00:00.500Z"),
        "s",
    )

    assert select_new(
        [fractional], since=whole["create_time"], self_id="users/me"
    ) == [fractional]
    assert newest_create_time([whole, fractional], None) == fractional["create_time"]


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


def test_recent_inbound_drops_own_and_bot_messages():
    messages = [
        parse_message(raw("a", "users/me", "my note", "2026-07-22T08:00:00Z"), "s"),
        parse_message(raw("b", "users/2", "hey you free?", "2026-07-22T08:30:00Z", dn="Sarah"), "s"),
        parse_message(raw("c", "users/bot", "build passed", "2026-07-22T08:45:00Z", stype="BOT"), "s"),
    ]
    gchat = FakeGChat(messages, self_id="users/me")
    inbound = gchat.recent_inbound("2026-07-22T00:00:00Z")
    assert [m["text"] for m in inbound] == ["hey you free?"]


def test_recent_inbound_has_global_cap_across_spaces():
    messages = [
        parse_message(
            raw(
                str(i),
                f"users/{i + 10}",
                f"message {i}",
                f"2026-07-22T10:{i:02d}:00Z",
            ),
            f"spaces/{i // 5}",
        )
        for i in range(30)
    ]
    gchat = FakeGChat(messages, self_id="users/me")

    inbound = gchat.recent_inbound("2026-07-22T00:00:00Z", per_space=5)

    assert len(inbound) == 25
    assert inbound[0]["text"] == "message 29"
    assert inbound[-1]["text"] == "message 5"


def test_failed_space_keeps_its_own_cursor_and_catches_up(conn):
    class SometimesFailingChat(FakeGChat):
        failed_spaces = {"spaces/B"}

        def list_spaces(self, max_results: int | None = None) -> list[dict]:
            return [{"name": "spaces/A"}, {"name": "spaces/B"}]

        def _space_messages(self, space_name, since_iso, per_space):
            if space_name in self.failed_spaces:
                raise RuntimeError("temporary room failure")
            return super()._space_messages(space_name, since_iso, per_space)

    old = "2026-07-22T09:00:00Z"
    kv_set(conn, CURSOR_KEY, old)  # simulate an upgrade from the global cursor
    messages = [
        parse_message(
            raw("a", "users/2", "from A", "2026-07-22T10:00:00Z"),
            "spaces/A",
        ),
        parse_message(
            raw("b", "users/3", "from B", "2026-07-22T10:05:00Z"),
            "spaces/B",
        ),
    ]
    gchat = SometimesFailingChat(messages)

    first = gchat.poll_new(conn)
    assert [m["text"] for m in first] == ["from A"]
    assert gchat.last_read_complete is False
    assert kv_get(conn, CURSOR_KEY) == old  # aggregate cursor did not skip B

    gchat.failed_spaces.clear()
    second = gchat.poll_new(conn)
    assert [m["text"] for m in second] == ["from B"]
    assert gchat.last_read_complete is True


def test_poll_pages_to_exhaustion_instead_of_capping_a_space(conn):
    class RecordingChat(FakeGChat):
        requested_limits: list[int | None] = []

        def _space_messages(self, space_name, since_iso, per_space):
            self.requested_limits.append(per_space)
            return super()._space_messages(space_name, since_iso, per_space)

    kv_set(conn, CURSOR_KEY, "2026-07-22T09:00:00Z")
    messages = [
        parse_message(
            raw(
                str(i),
                f"users/{i + 2}",
                f"message {i}",
                f"2026-07-22T10:{i // 60:02d}:{i % 60:02d}Z",
            ),
            "spaces/A",
        )
        for i in range(501)
    ]
    gchat = RecordingChat(messages, self_id="users/me")

    assert len(gchat.poll_new(conn)) == 501
    assert gchat.requested_limits == [None]


def test_api_client_follows_space_and_message_page_tokens():
    class Result:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class MessagesResource:
        def __init__(self):
            self.calls = []

        def list(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("pageToken") == "messages-2":
                return Result({"messages": [raw("m2", "users/3", "two", "2026-07-22T10:01:00Z")]})
            return Result(
                {
                    "messages": [raw("m1", "users/2", "one", "2026-07-22T10:00:00Z")],
                    "nextPageToken": "messages-2",
                }
            )

    class SpacesResource:
        def __init__(self):
            self.calls = []
            self.message_resource = MessagesResource()

        def list(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("pageToken") == "spaces-2":
                return Result({"spaces": [{"name": "spaces/B"}]})
            return Result(
                {
                    "spaces": [{"name": "spaces/A"}],
                    "nextPageToken": "spaces-2",
                }
            )

        def messages(self):
            return self.message_resource

    class Service:
        def __init__(self):
            self.resource = SpacesResource()

        def spaces(self):
            return self.resource

    service = Service()
    gchat = GChatClient(creds=object(), self_id="users/me", _svc=service)

    assert [space["name"] for space in gchat.list_spaces()] == [
        "spaces/A",
        "spaces/B",
    ]
    messages = gchat._space_messages("spaces/A", None, None)

    assert [message["text"] for message in messages] == ["one", "two"]
    assert [call.get("pageToken") for call in service.resource.calls] == [
        None,
        "spaces-2",
    ]
    assert [
        call.get("pageToken") for call in service.resource.message_resource.calls
    ] == [None, "messages-2"]


async def test_interactive_tool_filters_self_and_warns_on_partial_read():
    class PartialChat(GChatClient):
        def recent_messages(self, since_iso=None, per_space=10):
            self.last_read_complete = False
            return [
                parse_message(
                    raw("mine", "users/me", "my reply", "2026-07-22T10:02:00Z"),
                    "spaces/A",
                ),
                parse_message(
                    raw(
                        "bot",
                        "users/bot",
                        "automated",
                        "2026-07-22T10:01:00Z",
                        stype="BOT",
                    ),
                    "spaces/A",
                ),
                parse_message(
                    raw(
                        "inbound",
                        "users/2",
                        "Can you review this?",
                        "2026-07-22T10:00:00Z",
                        dn="Sarah",
                    ),
                    "spaces/A",
                ),
            ]

    registry = ToolRegistry()
    gchat_tools.register(
        registry,
        PartialChat(creds=object(), self_id="users/me"),
    )

    result, is_error = await registry.execute("gchat_recent", {})

    assert is_error is False
    assert "only partially checked" in result
    assert "Sarah: Can you review this?" in result
    assert "my reply" not in result
    assert "automated" not in result
