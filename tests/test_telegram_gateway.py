"""Telegram transport formatting and length safety."""

from types import SimpleNamespace

import pytest

from samantha.telegram_gateway import (
    TELEGRAM_CHUNK_CHARS,
    TelegramGateway,
    split_telegram_text,
    telegram_html,
)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_markdown_bold_is_rendered_without_exposing_unsafe_html():
    rendered = telegram_html("**Rebecca** says <send secrets>")

    assert rendered == "<b>Rebecca</b> says &lt;send secrets&gt;"


def test_splitter_stays_below_transport_limit():
    chunks = split_telegram_text(("long line of text\n" * 500))

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_CHUNK_CHARS for chunk in chunks)


async def test_keyboard_is_attached_only_to_final_chunk(settings):
    settings.dry_run = False
    settings.telegram_chat_id = 123
    gateway = TelegramGateway(settings)
    bot = FakeBot()
    gateway.app = SimpleNamespace(bot=bot)
    keyboard = object()

    await gateway.send("x " * 4_000, reply_markup=keyboard)

    assert len(bot.calls) > 1
    assert all(call["reply_markup"] is None for call in bot.calls[:-1])
    assert bot.calls[-1]["reply_markup"] is keyboard
    assert all(call["parse_mode"] == "HTML" for call in bot.calls)


class FakeReplyMessage:
    def __init__(self, text: str = "hello", *, fail_on: int | None = None) -> None:
        self.text = text
        self.fail_on = fail_on
        self.calls: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.calls.append({"text": text, **kwargs})
        if self.fail_on == len(self.calls):
            raise ConnectionError("Telegram rejected chunk")


async def test_reactive_reply_is_logged_only_after_every_chunk_succeeds(settings):
    settings.telegram_chat_id = 123
    delivered: list[tuple[str, str]] = []

    async def answer(_text: str) -> str:
        return "x " * 4_000

    gateway = TelegramGateway(
        settings,
        on_message=answer,
        on_delivered=lambda text, channel: delivered.append((text, channel)),
    )
    message = FakeReplyMessage(fail_on=2)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=123),
        message=message,
    )

    with pytest.raises(ConnectionError):
        await gateway._handle_message(update, None)

    assert delivered == []  # no phantom full reply in durable memory

    message.fail_on = None
    message.calls.clear()
    await gateway._handle_message(update, None)

    assert len(delivered) == 1
    assert delivered[0][1] == "telegram_reply"


async def test_callback_action_and_reply_are_both_recorded(settings):
    settings.telegram_chat_id = 123
    received: list[str] = []
    delivered: list[tuple[str, str]] = []

    async def callback(_data: str) -> str:
        return "Sent ✓"

    gateway = TelegramGateway(
        settings,
        on_callback=callback,
        on_callback_received=received.append,
        on_delivered=lambda text, channel: delivered.append((text, channel)),
    )
    message = FakeReplyMessage()

    class Query:
        data = "act:send:7"

        def __init__(self) -> None:
            self.message = message
            self.answered = False

        async def answer(self) -> None:
            self.answered = True

    query = Query()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=123),
        callback_query=query,
    )

    await gateway._handle_callback(update, None)

    assert query.answered is True
    assert received == ["act:send:7"]
    assert delivered == [("Sent ✓", "telegram_callback")]
    assert message.calls[0]["parse_mode"] == "HTML"
