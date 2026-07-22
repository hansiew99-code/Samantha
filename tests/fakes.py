"""Fake Anthropic client for offline tests: plays back a scripted sequence of
responses and records every request."""

from __future__ import annotations

from dataclasses import dataclass, field

@dataclass
class FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    id: str = "toolu_test"


def text_block(text: str) -> FakeBlock:
    return FakeBlock(type="text", text=text)


def tool_use(name: str, tool_input: dict, block_id: str = "toolu_1") -> FakeBlock:
    return FakeBlock(type="tool_use", name=name, input=tool_input, id=block_id)


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"
    usage: FakeUsage = field(default_factory=FakeUsage)


class _FakeMessages:
    def __init__(self, script: list[FakeResponse | Exception]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def create(self, **kwargs) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("fake client script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropicClient:
    """Plays back a scripted sequence of responses and records every request."""

    def __init__(self, script: list[FakeResponse | Exception]) -> None:
        self.messages = _FakeMessages(script)

    @property
    def calls(self) -> list[dict]:
        return self.messages.calls
