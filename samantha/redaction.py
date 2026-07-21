"""Redact credentials before they land anywhere durable.

The owner will inevitably paste a key or token into the chat one day ("here's
my ClickUp token"). Without this, that secret would be written verbatim to the
`messages` table, replayed into every future prompt, and swept into the nightly
consolidation. We mask known-shape secrets at the logging boundary so they
never persist.
"""

from __future__ import annotations

import re

# Each pattern matches a specific, high-confidence secret shape — deliberately
# prefix-anchored to avoid mangling ordinary prose.
_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),            # Anthropic API key
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),          # Slack bot/user tokens
    re.compile(r"xapp-[0-9]-[A-Za-z0-9-]{10,}"),          # Slack app-level token
    re.compile(r"gh[posru]_[A-Za-z0-9]{20,}"),            # GitHub tokens
    re.compile(r"ya29\.[A-Za-z0-9_-]{20,}"),              # Google OAuth access token
    re.compile(r"\bpk_[0-9]+_[A-Za-z0-9]+\b"),            # ClickUp personal token
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),         # Telegram bot token
]

_MASK = "[redacted-secret]"


def redact(text: str) -> str:
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(_MASK, text)
    return text
