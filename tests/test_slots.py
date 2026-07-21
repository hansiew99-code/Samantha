"""Free/busy slot intersection — pure code, the LLM only phrases results."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from samantha.integrations.gcal import find_free_slots, merge_intervals

TZ = ZoneInfo("Asia/Kuala_Lumpur")


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=TZ)


def test_merge_overlapping_intervals():
    merged = merge_intervals([
        (dt(22, 10), dt(22, 11)),
        (dt(22, 10, 30), dt(22, 12)),
        (dt(22, 14), dt(22, 15)),
    ])
    assert merged == [(dt(22, 10), dt(22, 12)), (dt(22, 14), dt(22, 15))]


def test_empty_calendar_offers_working_hours_start():
    slots = find_free_slots([], dt(22, 0), dt(22, 23), timedelta(minutes=60))
    assert slots[0] == (dt(22, 9), dt(22, 10))


def test_busy_blocks_are_avoided():
    busy = [(dt(22, 9), dt(22, 12)), (dt(22, 13), dt(22, 17, 30))]
    slots = find_free_slots(busy, dt(22, 0), dt(22, 23), timedelta(minutes=60))
    # Only 12:00-13:00 fits a full hour.
    assert slots == [(dt(22, 12), dt(22, 13))]


def test_multi_attendee_busy_is_combined():
    # Person A busy morning, person B busy afternoon → only midday gap works.
    busy_a = [(dt(22, 9), dt(22, 11))]
    busy_b = [(dt(22, 12), dt(22, 18))]
    slots = find_free_slots(busy_a + busy_b, dt(22, 0), dt(22, 23), timedelta(minutes=60))
    assert slots == [(dt(22, 11), dt(22, 12))]


def test_no_slot_when_duration_does_not_fit():
    busy = [(dt(22, 9), dt(22, 17, 45))]
    slots = find_free_slots(busy, dt(22, 0), dt(22, 23), timedelta(minutes=30))
    assert slots == []


def test_slots_span_multiple_days():
    busy = [(dt(22, 9), dt(22, 18))]  # day 1 fully busy
    slots = find_free_slots(busy, dt(22, 0), dt(23, 23), timedelta(minutes=60))
    assert slots[0][0].day == 23
    assert slots[0][0].hour == 9


def test_window_start_respected_over_working_hours():
    # Searching from 14:00 must not offer morning slots.
    slots = find_free_slots([], dt(22, 14), dt(22, 23), timedelta(minutes=60))
    assert slots[0][0] >= dt(22, 14)


def test_max_results_cap():
    slots = find_free_slots([], dt(22, 0), dt(28, 23), timedelta(minutes=60), max_results=3)
    assert len(slots) == 3
