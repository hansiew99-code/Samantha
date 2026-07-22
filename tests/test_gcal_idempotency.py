"""Calendar inserts converge after ambiguous provider outcomes/retries."""

from samantha.integrations.gcal import GCalClient


class Conflict(Exception):
    status_code = 409


class Request:
    def __init__(self, func):
        self.func = func

    def execute(self):
        return self.func()


class Events:
    def __init__(self):
        self.stored: dict[str, dict] = {}
        self.insert_ids: list[str] = []

    def insert(self, *, body, **_kwargs):
        def execute():
            event_id = body["id"]
            self.insert_ids.append(event_id)
            if event_id in self.stored:
                raise Conflict("event already exists")
            event = {**body, "htmlLink": f"https://calendar/{event_id}"}
            self.stored[event_id] = event
            return event

        return Request(execute)

    def get(self, *, eventId, **_kwargs):
        return Request(lambda: self.stored[eventId])


class Service:
    def __init__(self):
        self.resource = Events()

    def events(self):
        return self.resource


def test_identical_calendar_insert_uses_stable_provider_id():
    client = GCalClient(creds=object(), tz="Asia/Kuala_Lumpur")
    service = Service()
    client._service = lambda: service

    first = client.create_event(
        "Focus",
        "2026-07-23T10:00:00+08:00",
        "2026-07-23T11:00:00+08:00",
        location="Desk",
    )
    second = client.create_event(
        "Focus",
        "2026-07-23T10:00:00+08:00",
        "2026-07-23T11:00:00+08:00",
        location="Desk",
    )

    assert first["id"] == second["id"]
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert len(service.resource.stored) == 1


def test_different_calendar_payload_gets_a_different_id():
    client = GCalClient(creds=object(), tz="Asia/Kuala_Lumpur")
    service = Service()
    client._service = lambda: service

    first = client.create_event(
        "Focus",
        "2026-07-23T10:00:00+08:00",
        "2026-07-23T11:00:00+08:00",
    )
    second = client.create_event(
        "Focus",
        "2026-07-23T11:00:00+08:00",
        "2026-07-23T12:00:00+08:00",
    )

    assert first["id"] != second["id"]
    assert len(service.resource.stored) == 2
