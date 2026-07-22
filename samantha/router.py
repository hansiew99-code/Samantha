"""Tiered model routing (BRIEF §4). Selection happens before any API call;
mid-loop upgrades happen through the `escalate` tool handled in brain.py.
"""

from __future__ import annotations

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-4-8"

TIER_ORDER = [HAIKU, SONNET, OPUS]

# Per-model request parameters (BRIEF: max_tokens caps 1024/2048/4096;
# no thinking on Haiku; adaptive thinking + effort on Sonnet/Opus).
MODEL_PARAMS: dict[str, dict] = {
    HAIKU: {"max_tokens": 1024},
    SONNET: {
        "max_tokens": 2048,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    },
    OPUS: {
        "max_tokens": 4096,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    },
}

_THINK_HARD_MARKERS = ("think hard", "think carefully", "think deeply", "take your time")

# Requests that mean "go gather across my stuff and reason about it" — a brief,
# a catch-up, a look spanning calendar + inbox + tasks. These are exactly the
# asks where Haiku phones it in, answering from stale context instead of pulling
# live data, so they start on Sonnet, which actually chains the tool calls.
_GATHER_MARKERS = (
    "brief",
    "catch me up",
    "catch up",
    "fill me in",
    "rundown",
    "run down",
    "recap",
    "what's my day",
    "whats my day",
    "what does my day",
    "what's on today",
    "whats on today",
    "what's on my plate",
    "whats on my plate",
    "how's my day",
    "hows my day",
    "my day look",
    "anything i should know",
    "anything i need to know",
    "anything urgent",
    "what's going on",
    "whats going on",
    "summarize my",
    "summarise my",
    "my schedule",
    "what's up with",
)


def pick_model(user_message: str, max_tier: str = OPUS) -> str:
    """Haiku by default; briefing/gather asks start on Sonnet so she actually
    pulls live data; an explicit "think hard" jumps straight to Opus.
    `max_tier` lets the governor cap the tier when the budget is tight."""
    lowered = user_message.lower()
    if any(m in lowered for m in _THINK_HARD_MARKERS):
        wanted = OPUS
    elif any(m in lowered for m in _GATHER_MARKERS):
        wanted = SONNET
    else:
        wanted = HAIKU
    return cap_tier(wanted, max_tier)


def cap_tier(model: str, max_tier: str) -> str:
    if TIER_ORDER.index(model) > TIER_ORDER.index(max_tier):
        return max_tier
    return model


def next_tier_up(model: str) -> str:
    idx = TIER_ORDER.index(model)
    return TIER_ORDER[min(idx + 1, len(TIER_ORDER) - 1)]
