"""Gmail polling never claims freshness after a partial provider read."""

from __future__ import annotations

from types import SimpleNamespace

from samantha.app import _poll_gmail_once
from samantha.db import kv_get, kv_set, sync_status
from samantha.integrations.gmail import (
    GmailClient,
    HISTORY_KEY,
    INGESTION_CHECKPOINT_KEY,
    THREAD_RESULT_CLIP,
)


class Result:
    def __init__(self, value=None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.value


class StubGmail(GmailClient):
    def __init__(self, service) -> None:
        super().__init__(creds=object())
        self.service = service

    def _service(self):
        return self.service


def test_non_404_history_error_keeps_cursor_and_marks_poll_incomplete(conn):
    class History:
        def list(self, **_kwargs):
            return Result(error=TimeoutError("provider unavailable"))

    class Users:
        def history(self):
            return History()

    class Service:
        def users(self):
            return Users()

    kv_set(conn, HISTORY_KEY, "history-1")
    gmail = StubGmail(Service())

    assert gmail.poll_new(conn) == []
    assert gmail.last_poll_complete is False
    assert kv_get(conn, HISTORY_KEY) == "history-1"


def test_metadata_failure_returns_completed_subset_without_advancing(conn):
    class History:
        def list(self, **_kwargs):
            return Result(
                {
                    "historyId": "history-2",
                    "history": [
                        {
                            "messagesAdded": [
                                {"message": {"id": "m1", "labelIds": ["INBOX"]}},
                                {"message": {"id": "m2", "labelIds": ["INBOX"]}},
                            ]
                        }
                    ],
                }
            )

    class Messages:
        def get(self, *, id, **_kwargs):
            if id == "m2":
                return Result(error=TimeoutError("metadata unavailable"))
            return Result(
                {
                    "id": id,
                    "threadId": "thread-1",
                    "snippet": "Can you review this?",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Sarah <sarah@example.com>"},
                            {"name": "Subject", "value": "Review"},
                        ]
                    },
                }
            )

    class Users:
        def history(self):
            return History()

        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    kv_set(conn, HISTORY_KEY, "history-1")
    gmail = StubGmail(Service())

    events = gmail.poll_new(conn)

    assert [event["id"] for event in events] == ["m1"]
    assert gmail.last_poll_complete is False
    assert kv_get(conn, HISTORY_KEY) == "history-1"


def test_deleted_message_404_does_not_wedge_history_cursor(conn):
    class Http404(Exception):
        resp = SimpleNamespace(status=404)

    class History:
        def list(self, **_kwargs):
            return Result({
                "historyId": "history-2",
                "history": [{"messagesAdded": [
                    {"message": {"id": "gone", "labelIds": ["INBOX"]}}
                ]}],
            })

    class Messages:
        def get(self, **_kwargs):
            return Result(error=Http404("deleted"))

    class Users:
        def history(self):
            return History()

        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    kv_set(conn, HISTORY_KEY, "history-1")
    gmail = StubGmail(Service())

    assert gmail.poll_new(conn) == []
    assert gmail.last_poll_complete is True
    assert kv_get(conn, HISTORY_KEY) == "history-2"


def test_search_pages_to_exhaustion_when_limit_is_none():
    class Messages:
        def __init__(self) -> None:
            self.list_calls: list[dict] = []

        def list(self, **kwargs):
            self.list_calls.append(kwargs)
            if kwargs.get("pageToken") == "page-2":
                return Result({"messages": [{"id": "m2"}]})
            return Result(
                {
                    "messages": [{"id": "m1"}],
                    "nextPageToken": "page-2",
                }
            )

        def get(self, *, id, **_kwargs):
            return Result(
                {
                    "id": id,
                    "threadId": f"thread-{id}",
                    "payload": {"headers": []},
                }
            )

    messages = Messages()

    class Users:
        def messages(self):
            return messages

    class Service:
        def users(self):
            return Users()

    gmail = StubGmail(Service())

    found = gmail.search("in:inbox", max_results=None)

    assert [item["id"] for item in found] == ["m1", "m2"]
    assert [call.get("pageToken") for call in messages.list_calls] == [
        None,
        "page-2",
    ]


def test_expired_history_reconciliation_uses_unbounded_search(conn):
    class Http404(Exception):
        resp = SimpleNamespace(status=404)

    class History:
        def list(self, **_kwargs):
            return Result(error=Http404("expired"))

    class Users:
        def history(self):
            return History()

        def getProfile(self, **_kwargs):
            return Result({"historyId": "history-new"})

    class Service:
        def users(self):
            return Users()

    class ReconcilingGmail(StubGmail):
        requested_limits: list[int | None]

        def __init__(self, service):
            super().__init__(service)
            self.requested_limits = []
            self.requested_queries: list[str] = []

        def search(self, query: str, max_results: int | None = 10):
            assert query.startswith("in:inbox after:")
            self.requested_queries.append(query)
            self.requested_limits.append(max_results)
            return [{"id": f"m{i}"} for i in range(501)]

    kv_set(conn, HISTORY_KEY, "history-expired")
    kv_set(conn, INGESTION_CHECKPOINT_KEY, "2026-07-22T00:00:00+00:00")
    # A later digest read must not narrow the ingestion backfill window.
    kv_set(conn, "sync.gmail.last_success", "2026-07-22T12:00:00+00:00")
    gmail = ReconcilingGmail(Service())

    events = gmail.poll_new(conn)

    assert len(events) == 501
    assert gmail.requested_limits == [None]
    assert gmail.requested_queries == ["in:inbox after:1784678400"]
    assert gmail.last_poll_complete is True
    assert kv_get(conn, HISTORY_KEY) == "history-new"


def test_long_thread_preserves_latest_message_in_bounded_result():
    messages = [
        {
            "snippet": f"message-{index}-" + ("x" * 1_200),
            "payload": {"headers": [
                {"name": "From", "value": f"person{index}@example.com"},
                {"name": "Subject", "value": "Long thread"},
            ]},
        }
        for index in range(6)
    ]

    class Threads:
        def get(self, **_kwargs):
            return Result({"messages": messages})

    class Users:
        def threads(self):
            return Threads()

    class Service:
        def users(self):
            return Users()

    rendered = StubGmail(Service()).read_thread("thread-1")

    assert len(rendered) <= THREAD_RESULT_CLIP
    assert "message-5-" in rendered
    assert "older middle messages omitted" in rendered


async def test_app_records_partial_failure_but_processes_returned_events(conn):
    message = {
        "id": "m1",
        "from": "Sarah <sarah@example.com>",
        "subject": "Review",
    }

    class PartialGmail:
        last_poll_complete = False

        def poll_new(self, _conn):
            return [message]

    observed: list[tuple[str, dict]] = []
    enqueued: list[tuple[str, str, str, dict]] = []
    app = SimpleNamespace(
        conn=conn,
        gmail=PartialGmail(),
        watchers=SimpleNamespace(
            observe=lambda source, payload: observed.append((source, payload))
        ),
        bus=SimpleNamespace(
            enqueue=lambda source, kind, scope, payload, **_kwargs: enqueued.append(
                (source, kind, scope, payload)
            )
        ),
    )

    await _poll_gmail_once(app)

    assert sync_status(conn, "gmail") == {
        "last_success": None,
        "last_error": "PartialGmailReadError",
    }
    assert observed == [("gmail", message)]
    assert enqueued == [
        ("gmail", "new_email", "Sarah <sarah@example.com>", message)
    ]
    assert kv_get(conn, INGESTION_CHECKPOINT_KEY) is None


async def test_complete_poll_advances_dedicated_ingestion_checkpoint(conn):
    class CompleteGmail:
        last_poll_complete = True

        def poll_new(self, _conn):
            return []

    app = SimpleNamespace(
        conn=conn,
        gmail=CompleteGmail(),
        watchers=SimpleNamespace(observe=lambda *_args: None),
        bus=SimpleNamespace(enqueue=lambda *_args: None),
    )

    await _poll_gmail_once(app)

    assert kv_get(conn, INGESTION_CHECKPOINT_KEY) is not None
    assert sync_status(conn, "gmail")["last_error"] is None
