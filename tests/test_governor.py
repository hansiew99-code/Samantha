"""Governor: price-table math, mode thresholds, deterministic-mode behavior."""

from __future__ import annotations

import pytest

from samantha.brain import Brain
from samantha.governor import (
    DEGRADED,
    DETERMINISTIC,
    NORMAL,
    Governor,
    Usage,
    price_usage,
)
from samantha.router import HAIKU, OPUS
from samantha.tools import ToolRegistry

from fakes import FakeAnthropicClient


# -- price math vs. hand-computed fixtures ------------------------------------


def test_haiku_price_math():
    usage = Usage(input_tokens=1000, output_tokens=500)
    # 1000/1M * $1 + 500/1M * $5 = 0.001 + 0.0025
    assert price_usage(HAIKU, usage) == pytest.approx(0.0035)


def test_cache_rates():
    usage = Usage(input_tokens=0, output_tokens=0,
                  cache_read_input_tokens=10_000, cache_creation_input_tokens=4_000)
    # reads at 0.1x input ($0.10/M), writes at 1.25x ($1.25/M)
    assert price_usage(HAIKU, usage) == pytest.approx(0.001 + 0.005)


def test_batch_discount_applies_to_in_out_only():
    usage = Usage(input_tokens=10_000, output_tokens=2_000)
    full = price_usage(HAIKU, usage)
    batched = price_usage(HAIKU, usage, batch=True)
    assert batched == pytest.approx(full * 0.5)


def test_opus_price_math():
    usage = Usage(input_tokens=4_000, output_tokens=800)
    # 4000/1M * $5 + 800/1M * $25 = 0.02 + 0.02
    assert price_usage(OPUS, usage) == pytest.approx(0.04)


def test_unknown_model_rejected():
    with pytest.raises(ValueError):
        price_usage("claude-nonexistent", Usage(input_tokens=1))


# -- mode thresholds ----------------------------------------------------------


def spend(gov: Governor, usd: float) -> None:
    # $1/MTok input on Haiku → tokens = usd * 1M
    gov.record(HAIKU, "chat", Usage(input_tokens=int(usd * 1_000_000)))


def test_mode_flips_at_70_and_100_percent(conn):
    gov = Governor(conn, daily_budget_usd=0.100)
    assert gov.mode() == NORMAL
    spend(gov, 0.069)
    assert gov.mode() == NORMAL
    assert gov.max_tier() == OPUS
    spend(gov, 0.002)  # 71%
    assert gov.mode() == DEGRADED
    assert gov.max_tier() == HAIKU
    spend(gov, 0.030)  # 101%
    assert gov.mode() == DETERMINISTIC


def test_spend_report_contains_the_numbers(conn):
    gov = Governor(conn, daily_budget_usd=0.177)
    spend(gov, 0.05)
    report = gov.spend_report()
    assert "$0.0500" in report and "mode: normal" in report and "haiku" in report


# -- deterministic mode: no LLM, reminders unaffected -------------------------


async def test_deterministic_mode_answers_without_api_call(settings, memory, conn):
    gov = Governor(conn, daily_budget_usd=0.001)
    spend(gov, 0.002)  # blow the budget
    client = FakeAnthropicClient(script=[])  # any API call would raise
    brain = Brain(settings, memory, ToolRegistry(), gov, client=client)

    reply = await brain.handle_message("hello?")

    assert "budget" in reply
    assert client.calls == []


async def test_reminders_fire_in_deterministic_mode(settings, conn):
    """The BRIEF §9 guarantee: budget exhaustion never silences reminders."""
    from samantha.reminders import ReminderService

    gov = Governor(conn, daily_budget_usd=0.001)
    spend(gov, 0.002)
    assert gov.mode() == DETERMINISTIC

    delivered = []

    async def deliver(rid: int, text: str) -> None:
        delivered.append(text)

    class NoopScheduler:
        def add_job(self, *a, **k):  # date-trigger registration is irrelevant here
            return None

        def get_job(self, *_a):
            return None

    svc = ReminderService(conn, NoopScheduler(), "Asia/Kuala_Lumpur", deliver=deliver)
    from datetime import datetime, timedelta, timezone

    rid = svc.set("Still here!", datetime.now(timezone.utc) + timedelta(minutes=1))
    await svc._fire(rid, overdue=False)

    assert delivered == ["Still here!"]
