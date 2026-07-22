"""Model routing: cheap by default, Sonnet for gather/brief asks, Opus on
demand, and the governor cap always wins (BRIEF §4)."""

from __future__ import annotations

import pytest

from samantha.router import HAIKU, OPUS, SONNET, pick_model


def test_default_is_haiku():
    assert pick_model("remember I like satay") == HAIKU
    assert pick_model("what's 2 + 2") == HAIKU


@pytest.mark.parametrize(
    "message",
    [
        "give me my morning brief",
        "brief me",
        "what's my day look like?",
        "catch me up",
        "fill me in on today",
        "anything I should know?",
        "what's on my plate today",
        "recap my week",
    ],
)
def test_gather_asks_route_to_sonnet(message):
    # These are exactly the requests where Haiku answers from stale context
    # instead of pulling the calendar/inbox — start them a tier up.
    assert pick_model(message) == SONNET


def test_think_hard_still_jumps_to_opus():
    assert pick_model("think hard about my week") == OPUS
    # Opus wins even when a gather marker is also present.
    assert pick_model("think carefully and give me a brief") == OPUS


def test_governor_cap_downgrades_the_wanted_tier():
    assert pick_model("morning brief", max_tier=HAIKU) == HAIKU
    assert pick_model("think hard", max_tier=SONNET) == SONNET
