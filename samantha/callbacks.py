"""Inline-button callback routing. Callback data format: "prefix:arg1:arg2...".

Button taps are zero-token operations (BRIEF §4.1) — handlers act directly on
the DB/services, never through the LLM.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

Handler = Callable[[list[str]], Awaitable[str | None]]


class CallbackRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, prefix: str, handler: Handler) -> None:
        self._handlers[prefix] = handler

    async def dispatch(self, data: str) -> str | None:
        prefix, *args = data.split(":")
        handler = self._handlers.get(prefix)
        if handler is None:
            log.warning("no callback handler for %r", data)
            return None
        return await handler(args)
