"""Typed owner-facing replies carried from the brain to the transport.

Most replies are plain strings.  A typed reply is used when delivery metadata
matters independently of the wording—for example, a degraded model response
must not be mistaken for a successful conversational answer in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReplyStatus = Literal["answered", "degraded"]


@dataclass(frozen=True)
class OwnerReply:
    text: str
    history_channel: str = "telegram_reply"
    status: ReplyStatus = "answered"

