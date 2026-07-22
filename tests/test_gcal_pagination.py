"""Calendar event windows are complete even when Google returns many pages."""

from __future__ import annotations

from samantha.integrations.gcal import GCalClient


class Result:
    def __init__(self, value: dict) -> None:
        self.value = value

    def execute(self) -> dict:
        return self.value


class EventsResource:
    def __init__(self, pages: dict[str | None, dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return Result(self.pages[kwargs.get("pageToken")])


class Service:
    def __init__(self, events: EventsResource) -> None:
        self._events = events

    def events(self) -> EventsResource:
        return self._events


class StubCalendar(GCalClient):
    def __init__(self, service: Service) -> None:
        super().__init__(creds=object(), tz="Asia/Kuala_Lumpur")
        self.service = service

    def _service(self):
        return self.service


def event(index: int) -> dict:
    return {
        "id": f"event-{index}",
        "summary": f"Meeting {index}",
        "start": {"dateTime": f"2026-07-23T{index % 24:02d}:00:00+08:00"},
        "end": {"dateTime": f"2026-07-23T{index % 24:02d}:30:00+08:00"},
    }


def test_list_events_follows_page_tokens_beyond_first_twenty():
    resource = EventsResource(
        {
            None: {"items": [event(i) for i in range(20)], "nextPageToken": "p2"},
            "p2": {"items": [event(i) for i in range(20, 23)]},
        }
    )
    calendar = StubCalendar(Service(resource))

    events = calendar.list_events(
        "2026-07-23T00:00:00+08:00",
        "2026-07-24T00:00:00+08:00",
    )

    assert [item["id"] for item in events] == [f"event-{i}" for i in range(23)]
    assert [call.get("pageToken") for call in resource.calls] == [None, "p2"]


def test_explicit_calendar_limit_remains_bounded():
    resource = EventsResource(
        {
            None: {"items": [event(i) for i in range(20)], "nextPageToken": "p2"},
            "p2": {"items": [event(i) for i in range(20, 23)]},
        }
    )
    calendar = StubCalendar(Service(resource))

    events = calendar.list_events("start", "end", max_results=20)

    assert len(events) == 20
    assert len(resource.calls) == 1
