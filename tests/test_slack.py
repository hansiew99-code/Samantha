"""Slack Socket Mode durability and idempotency."""

from types import SimpleNamespace

import pytest

from samantha.integrations.slack import SlackService
from samantha.db import sync_status


class FakeClient:
    def __init__(self, order):
        self.order = order
        self.responses = []

    def send_socket_mode_response(self, response):
        self.order.append("ack")
        self.responses.append(response)


class FakeBus:
    def __init__(self, order, error=None):
        self.order = order
        self.error = error
        self.calls = []

    def enqueue(self, *args, **kwargs):
        self.order.append("enqueue")
        if self.error:
            raise self.error
        self.calls.append((args, kwargs))
        return 1


def make_service(bus):
    service = object.__new__(SlackService)
    service.bus = bus
    service.self_user_id = "U-BOT"
    return service


def mention_request():
    return SimpleNamespace(
        type="events_api",
        envelope_id="env-1",
        payload={
            "event_id": "Ev-1",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": "hello",
                "ts": "1.0",
            },
        },
    )


def test_slack_persists_before_acknowledging():
    order = []
    bus = FakeBus(order)
    client = FakeClient(order)

    make_service(bus)._on_request(client, mention_request())

    assert order == ["enqueue", "ack"]
    assert bus.calls[0][1]["dedupe_key"] == "slack:Ev-1"


def test_slack_does_not_ack_when_durable_enqueue_fails():
    order = []
    bus = FakeBus(order, error=RuntimeError("database unavailable"))
    client = FakeClient(order)

    with pytest.raises(RuntimeError):
        make_service(bus)._on_request(client, mention_request())

    assert order == ["enqueue"]
    assert client.responses == []


def test_slack_socket_failure_degrades_without_aborting_daemon(conn):
    class Web:
        def auth_test(self):
            return {"user_id": "U-BOT"}

    class Socket:
        def __init__(self):
            self.socket_mode_request_listeners = []
            self.closed = False

        def connect(self):
            raise RuntimeError("websocket unavailable")

        def close(self):
            self.closed = True

    service = object.__new__(SlackService)
    service.web = Web()
    service.socket = Socket()
    service.bus = SimpleNamespace(conn=conn)
    service.self_user_id = None

    service.start()  # must not raise and take down Telegram/reminders

    assert service.socket.closed is True
    assert sync_status(conn, "slack")["last_error"] == "RuntimeError"
