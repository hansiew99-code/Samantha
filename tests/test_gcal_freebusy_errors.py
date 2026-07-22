"""Free/busy must never turn provider errors into fabricated availability."""

import pytest

from samantha.integrations.gcal import GCalClient


class Result:
    def execute(self):
        return {
            "calendars": {
                "owner@example.com": {"busy": []},
                "guest@example.com": {
                    "errors": [{"reason": "notFound"}],
                    "busy": [],
                },
            }
        }


class FreeBusy:
    def query(self, **_kwargs):
        return Result()


class Service:
    def freebusy(self):
        return FreeBusy()


class Calendar(GCalClient):
    def _service(self):
        return Service()


def test_freebusy_raises_when_any_calendar_was_not_checked():
    calendar = Calendar(creds=object(), tz="Asia/Kuala_Lumpur")

    with pytest.raises(RuntimeError, match="guest@example.com.*notFound"):
        calendar.freebusy(
            ["owner@example.com", "guest@example.com"],
            "2026-07-23T09:00:00+08:00",
            "2026-07-23T18:00:00+08:00",
        )
