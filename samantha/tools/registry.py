"""Tool registry. The spec list is sorted by name and byte-identical between
requests — a varying tool set would invalidate the whole prompt cache
(tools render at position 0 of the prefix).
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 4000  # truncate tool results — context is money


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    func: Callable[..., Any]  # sync or async; kwargs from tool input

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def specs(self) -> list[dict]:
        return [self._tools[name].spec() for name in sorted(self._tools)]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    async def execute(self, name: str, tool_input: dict) -> tuple[str, bool]:
        """Returns (content, is_error). Exceptions become error results the
        model can recover from rather than crashing the loop."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}", True
        try:
            result = tool.func(**(tool_input or {}))
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            if len(result) > MAX_RESULT_CHARS:
                result = result[:MAX_RESULT_CHARS] + "\n[truncated]"
            return result, False
        except Exception as exc:  # noqa: BLE001 — surface to the model
            log.exception("tool %s failed", name)
            return f"Error in {name}: {exc}", True
