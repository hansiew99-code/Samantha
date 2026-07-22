"""Conditional watchers: durable follow-through without LLM calls."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from samantha.tools import ToolRegistry
from samantha.tools.watcher_tools import register as register_watcher_tools
from samantha.watchers import WatcherService

TZ = "Asia/Kuala_Lumpur"


class StubVerifier:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result
        self.error: Exception | None = None
        self.calls: list[dict] = []

    def __call__(self, criteria: dict) -> dict | None:
        self.calls.append(criteria)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
async def scheduler():
    instance = AsyncIOScheduler(timezone=TZ, event_loop=asyncio.get_running_loop())
    instance.start()
    yield instance
    instance.shutdown(wait=False)


def future_times() -> tuple[datetime, datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=1), now + timedelta(hours=1), now + timedelta(hours=2)


def email_at(when: datetime, *, sender: str = "Shyan <shyan@example.com>") -> dict:
    return {
        "id": "gmail-message-1",
        "from": sender,
        "subject": "Colour task list",
        "internal_date": str(int(when.timestamp() * 1000)),
    }


def make_service(conn, scheduler, verifier, delivered=None) -> WatcherService:
    async def deliver(watcher_id: int, text: str) -> None:
        if delivered is not None:
            delivered.append((watcher_id, text))

    service = WatcherService(
        conn,
        scheduler,
        TZ,
        deliver=deliver if delivered is not None else None,
    )
    service.register_verifier("gmail", verifier)
    return service


def set_watch(service: WatcherService, **overrides) -> int:
    since, expected_by, notify_at = future_times()
    values = {
        "description": "Shyan's promised email",
        "sender_contains": "Shyan",
        "subject_contains": "Colour",
        "since": since,
        "expected_by": expected_by,
        "notify_at": notify_at,
        "fallback_text": "Shyan's email still hasn't arrived—check in with her.",
        "late_text": "Shyan's email arrived late, but it is here now.",
    }
    values.update(overrides)
    return service.set_email(**values)


async def test_absent_on_create_is_saved_and_watched_without_llm_spend(conn, scheduler):
    verifier = StubVerifier(result=None)
    service = make_service(conn, scheduler, verifier)
    registry = ToolRegistry()
    register_watcher_tools(registry, service)
    _since, expected_by, notify_at = future_times()

    receipt, is_error = await registry.execute(
        "watchers_set_email",
        {
            "description": "Shyan's promised email",
            "sender_contains": "Shyan",
            "subject_contains": "Colour",
            "expected_by": expected_by.isoformat(),
            "notify_at": notify_at.isoformat(),
            "fallback_text": "Check in with Shyan.",
        },
    )

    assert is_error is False
    assert "Checked—nothing matching from Shyan yet" in receipt
    row = conn.execute("SELECT * FROM watchers").fetchone()
    assert row["status"] == "active"
    assert scheduler.get_job(f"watcher-{row['id']}-deadline") is not None
    assert scheduler.get_job(f"watcher-{row['id']}-notify") is not None
    # Creating and verifying a watch is deterministic integration work.
    assert conn.execute("SELECT COUNT(*) FROM spend_log").fetchone()[0] == 0


async def test_on_time_email_resolves_watch_and_removes_jobs(conn, scheduler):
    verifier = StubVerifier()
    service = make_service(conn, scheduler, verifier)
    since, expected_by, notify_at = future_times()
    watcher_id = set_watch(
        service,
        since=since,
        expected_by=expected_by,
        notify_at=notify_at,
    )

    matched = service.observe("gmail", email_at(expected_by - timedelta(minutes=5)))

    assert matched == [watcher_id]
    row = conn.execute("SELECT * FROM watchers WHERE id = ?", (watcher_id,)).fetchone()
    assert row["status"] == "resolved"
    assert json.loads(row["resolution_payload"])["id"] == "gmail-message-1"
    assert scheduler.get_job(f"watcher-{watcher_id}-deadline") is None
    assert scheduler.get_job(f"watcher-{watcher_id}-notify") is None


async def test_missing_deadline_breaches_then_delivers_fallback(conn, scheduler):
    delivered: list[tuple[int, str]] = []
    verifier = StubVerifier(result=None)
    service = make_service(conn, scheduler, verifier, delivered)
    watcher_id = set_watch(service)

    await service._evaluate_deadline(watcher_id)
    row = conn.execute("SELECT * FROM watchers WHERE id = ?", (watcher_id,)).fetchone()
    assert row["status"] == "breached"
    assert row["breached_at"] is not None

    await service._evaluate_notify(watcher_id)

    row = conn.execute("SELECT * FROM watchers WHERE id = ?", (watcher_id,)).fetchone()
    assert row["status"] == "fired"
    assert delivered == [
        (watcher_id, "Shyan's email still hasn't arrived—check in with her.")
    ]
    assert scheduler.get_job(f"watcher-{watcher_id}-deadline") is None
    assert scheduler.get_job(f"watcher-{watcher_id}-notify") is None
    assert conn.execute("SELECT COUNT(*) FROM spend_log").fetchone()[0] == 0


async def test_late_arrival_uses_late_message_instead_of_missing_claim(conn, scheduler):
    delivered: list[tuple[int, str]] = []
    verifier = StubVerifier()
    service = make_service(conn, scheduler, verifier, delivered)
    since, expected_by, notify_at = future_times()
    watcher_id = set_watch(
        service,
        since=since,
        expected_by=expected_by,
        notify_at=notify_at,
    )
    late_email = email_at(expected_by + timedelta(minutes=5))
    verifier.result = late_email

    assert service.observe("gmail", late_email) == [watcher_id]
    assert conn.execute(
        "SELECT status FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()["status"] == "breached"

    await service._evaluate_notify(watcher_id)

    assert delivered == [(watcher_id, "Shyan's email arrived late, but it is here now.")]
    assert conn.execute(
        "SELECT status FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()["status"] == "fired"


async def test_verifier_failure_retries_without_false_missing_claim(conn, scheduler):
    delivered: list[tuple[int, str]] = []
    verifier = StubVerifier()
    verifier.error = RuntimeError("temporary Gmail outage")
    service = make_service(conn, scheduler, verifier, delivered)
    registry = ToolRegistry()
    register_watcher_tools(registry, service)
    _since, expected_by, notify_at = future_times()

    receipt, is_error = await registry.execute(
        "watchers_set_email",
        {
            "description": "Shyan's promised email",
            "sender_contains": "Shyan",
            "expected_by": expected_by.isoformat(),
            "notify_at": notify_at.isoformat(),
            "fallback_text": "Check in with Shyan.",
        },
    )
    watcher_id = conn.execute("SELECT id FROM watchers").fetchone()["id"]

    assert is_error is False
    assert "Gmail wasn't reachable" in receipt
    assert "won't claim the email is missing" in receipt

    await service._evaluate_deadline(watcher_id)
    assert conn.execute(
        "SELECT status FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()["status"] == "active"
    assert scheduler.get_job(f"watcher-{watcher_id}-deadline") is not None

    await service._evaluate_notify(watcher_id)
    await service._evaluate_notify(watcher_id)  # repeated outage: notify only once

    row = conn.execute("SELECT status FROM watchers WHERE id = ?", (watcher_id,)).fetchone()
    assert row["status"] == "active"
    assert len(delivered) == 1
    assert "couldn't verify" in delivered[0][1]
    assert "keeping the watch open" in delivered[0][1]
    assert "Check in with Shyan" not in delivered[0][1]
    assert scheduler.get_job(f"watcher-{watcher_id}-notify") is not None


async def test_rehydrate_restores_active_and_breached_watches(conn, scheduler):
    verifier = StubVerifier(result=None)
    first = make_service(conn, scheduler, verifier)
    active_id = set_watch(first, sender_contains="Shyan")
    breached_id = set_watch(first, sender_contains="Rebecca")
    conn.execute("UPDATE watchers SET status = 'breached' WHERE id = ?", (breached_id,))
    conn.commit()
    scheduler.remove_all_jobs()

    restored = make_service(conn, scheduler, verifier)
    assert restored.rehydrate() == 2

    assert scheduler.get_job(f"watcher-{active_id}-deadline") is not None
    assert scheduler.get_job(f"watcher-{active_id}-notify") is not None
    assert scheduler.get_job(f"watcher-{breached_id}-deadline") is None
    assert scheduler.get_job(f"watcher-{breached_id}-notify") is not None


async def test_duplicate_default_since_is_idempotent(conn, scheduler):
    verifier = StubVerifier(result=None)
    service = make_service(conn, scheduler, verifier)
    _since, expected_by, notify_at = future_times()

    first_id = set_watch(
        service,
        since=None,
        expected_by=expected_by,
        notify_at=notify_at,
    )
    second_id = set_watch(
        service,
        since=None,
        expected_by=expected_by,
        notify_at=notify_at,
    )

    assert second_id == first_id
    assert conn.execute("SELECT COUNT(*) FROM watchers").fetchone()[0] == 1
    assert len(scheduler.get_jobs()) == 2
    criteria = json.loads(
        conn.execute("SELECT criteria FROM watchers WHERE id = ?", (first_id,)).fetchone()[0]
    )
    # Asia/Kuala_Lumpur's local midnight is 16:00 UTC on the previous day.
    assert datetime.fromisoformat(criteria["since"]).astimezone(timezone.utc).hour == 16


async def test_retry_repairs_a_partially_scheduled_watch(
    conn, scheduler, monkeypatch
):
    verifier = StubVerifier(result=None)
    service = make_service(conn, scheduler, verifier)
    since, expected_by, notify_at = future_times()
    original_notify = service._schedule_notify
    attempts = 0

    def fail_once(watcher_id, run_at):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("scheduler temporarily unavailable")
        original_notify(watcher_id, run_at)

    monkeypatch.setattr(service, "_schedule_notify", fail_once)
    with pytest.raises(RuntimeError):
        set_watch(
            service,
            since=since,
            expected_by=expected_by,
            notify_at=notify_at,
        )

    watcher_id = conn.execute("SELECT id FROM watchers").fetchone()[0]
    assert scheduler.get_job(f"watcher-{watcher_id}-deadline") is not None
    assert scheduler.get_job(f"watcher-{watcher_id}-notify") is None

    retried_id = set_watch(
        service,
        since=since,
        expected_by=expected_by,
        notify_at=notify_at,
    )
    assert retried_id == watcher_id
    assert scheduler.get_job(f"watcher-{watcher_id}-deadline") is not None
    assert scheduler.get_job(f"watcher-{watcher_id}-notify") is not None


async def test_missing_at_notify_policy_does_not_create_a_false_breach(conn, scheduler):
    verifier = StubVerifier(result=None)
    service = make_service(conn, scheduler, verifier)
    since, expected_by, notify_at = future_times()
    watcher_id = set_watch(
        service,
        since=since,
        expected_by=expected_by,
        notify_at=notify_at,
        deadline_policy="missing_at_notify",
    )

    assert scheduler.get_job(f"watcher-{watcher_id}-deadline") is None
    await service._evaluate_deadline(watcher_id)
    row = conn.execute(
        "SELECT status FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()
    assert row["status"] == "active"

    arrived_late = email_at(expected_by + timedelta(minutes=5))
    assert service.observe("gmail", arrived_late) == [watcher_id]
    assert conn.execute(
        "SELECT status FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()["status"] == "resolved"


async def test_failed_telegram_delivery_keeps_watch_retryable(conn, scheduler):
    attempts = 0

    async def flaky_deliver(_watcher_id: int, _text: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary Telegram outage")

    verifier = StubVerifier(result=None)
    service = WatcherService(conn, scheduler, TZ, deliver=flaky_deliver)
    service.register_verifier("gmail", verifier)
    watcher_id = set_watch(service)
    await service._evaluate_deadline(watcher_id)

    await service._evaluate_notify(watcher_id)

    assert conn.execute(
        "SELECT status FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()["status"] == "breached"
    assert scheduler.get_job(f"watcher-{watcher_id}-notify") is not None

    await service._evaluate_notify(watcher_id)

    assert attempts == 2
    assert conn.execute(
        "SELECT status FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()["status"] == "fired"


async def test_failed_outage_notice_is_not_marked_sent_and_retries(conn, scheduler):
    attempts = 0

    async def flaky_deliver(_watcher_id: int, _text: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Telegram unavailable")

    verifier = StubVerifier()
    verifier.error = RuntimeError("Gmail unavailable")
    service = WatcherService(conn, scheduler, TZ, deliver=flaky_deliver)
    service.register_verifier("gmail", verifier)
    watcher_id = set_watch(service)

    await service._evaluate_notify(watcher_id)

    row = conn.execute(
        "SELECT resolution_payload FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()
    assert not row["resolution_payload"]
    assert scheduler.get_job(f"watcher-{watcher_id}-notify") is not None

    await service._evaluate_notify(watcher_id)

    payload = json.loads(conn.execute(
        "SELECT resolution_payload FROM watchers WHERE id = ?", (watcher_id,)
    ).fetchone()[0])
    assert attempts == 2
    assert payload["verification_error_notified"] is True
