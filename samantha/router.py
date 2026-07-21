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


def pick_model(user_message: str, max_tier: str = OPUS) -> str:
    """Haiku by default; explicit user request jumps straight to Opus.
    `max_tier` lets the governor cap escalation when the budget is tight."""
    lowered = user_message.lower()
    wanted = OPUS if any(m in lowered for m in _THINK_HARD_MARKERS) else HAIKU
    return cap_tier(wanted, max_tier)


def cap_tier(model: str, max_tier: str) -> str:
    if TIER_ORDER.index(model) > TIER_ORDER.index(max_tier):
        return max_tier
    return model


def next_tier_up(model: str) -> str:
    idx = TIER_ORDER.index(model)
    return TIER_ORDER[min(idx + 1, len(TIER_ORDER) - 1)]
