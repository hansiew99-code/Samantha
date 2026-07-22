"""Telegram gateway: long polling (no public URL), strict single-user allowlist.

The gateway is transport only — it hands text to an injected `on_message`
coroutine and button taps to `on_callback`, and exposes `send()` for proactive
pushes. Phase 0 wires an echo handler; the brain replaces it in Phase 1.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Awaitable, Callable

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .replies import OwnerReply

log = logging.getLogger(__name__)

TELEGRAM_CHUNK_CHARS = 3_500


def split_telegram_text(text: str, limit: int = TELEGRAM_CHUNK_CHARS) -> list[str]:
    """Split at natural boundaries under Telegram's 4,096-character limit."""
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def telegram_html(text: str) -> str:
    """Render the small Markdown subset models naturally emit, safely.

    Everything is HTML-escaped first; only balanced bold/code pairs are then
    introduced.  This removes visible ``**name**`` bot syntax without letting
    arbitrary model or email HTML become Telegram markup.
    """
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    return escaped

Reply = str | OwnerReply
OnMessage = Callable[[str], Awaitable[Reply | None]]
OnCallback = Callable[[str], Awaitable[Reply | None]]
OnDelivered = Callable[[str, str], None]
OnCallbackReceived = Callable[[str], None]


class TelegramGateway:
    def __init__(
        self,
        settings: Settings,
        on_message: OnMessage | None = None,
        on_callback: OnCallback | None = None,
        on_delivered: OnDelivered | None = None,
        on_callback_received: OnCallbackReceived | None = None,
    ) -> None:
        self.settings = settings
        self.on_message = on_message or self._echo
        self.on_callback = on_callback
        self.on_delivered = on_delivered
        self.on_callback_received = on_callback_received
        self.commands: dict[str, OnMessage] = {}
        self.app: Application | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self.app = (
            ApplicationBuilder().token(self.settings.telegram_bot_token).build()
        )
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        for name in self.commands:
            self.app.add_handler(CommandHandler(name, self._dispatch_command))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        await self.app.initialize()
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(drop_pending_updates=False)
        log.info("telegram gateway polling")

    async def stop(self) -> None:
        if self.app is None:
            return
        if self.app.updater is not None:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        log.info("telegram gateway stopped")

    def register_command(self, name: str, handler: OnMessage) -> None:
        """Register /name → handler. Call before start()."""
        self.commands[name] = handler

    # -- outbound ------------------------------------------------------------

    async def send(self, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        if self.settings.dry_run:
            log.info("[dry-run] telegram send: %s", text)
            return
        if self.app is None:
            raise RuntimeError("Telegram gateway is not initialized")
        chunks = split_telegram_text(text)
        for index, chunk in enumerate(chunks):
            await self.app.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=telegram_html(chunk),
                parse_mode="HTML",
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )

    # -- inbound -------------------------------------------------------------

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        ok = chat is not None and chat.id == self.settings.telegram_chat_id
        if not ok and chat is not None:
            log.warning("ignoring message from unauthorized chat %s", chat.id)
        return ok

    async def _handle_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or update.message is None or not update.message.text:
            return
        reply = await self.on_message(update.message.text)
        if reply:
            text, channel = self._reply_payload(reply, "telegram_reply")
            for chunk in split_telegram_text(text):
                await update.message.reply_text(
                    telegram_html(chunk), parse_mode="HTML"
                )
            self._record_delivered(text, channel)

    async def _handle_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or update.callback_query is None:
            return
        query = update.callback_query
        await query.answer()
        if self.on_callback and query.data:
            if self.on_callback_received:
                try:
                    self.on_callback_received(query.data)
                except Exception:
                    # The tap already happened; do not make Telegram retry and
                    # risk executing an idempotent-but-external callback twice.
                    log.exception("could not persist Telegram callback input")
            reply = await self.on_callback(query.data)
            if reply and query.message is not None:
                text, channel = self._reply_payload(reply, "telegram_callback")
                for chunk in split_telegram_text(text):
                    await query.message.reply_text(
                        telegram_html(chunk), parse_mode="HTML"
                    )
                self._record_delivered(text, channel)

    async def _dispatch_command(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or update.message is None or not update.message.text:
            return
        name = update.message.text.lstrip("/").split()[0].split("@")[0]
        handler = self.commands.get(name)
        if handler:
            reply = await handler(update.message.text)
            if reply:
                text, channel = self._reply_payload(reply, "telegram_command")
                for chunk in split_telegram_text(text):
                    await update.message.reply_text(
                        telegram_html(chunk), parse_mode="HTML"
                    )
                self._record_delivered(text, channel)

    async def _cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or update.message is None:
            return
        await update.message.reply_text("Hi, I'm Samantha. I'm here — talk to me.")

    async def _cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or update.message is None:
            return
        cmds = "\n".join(f"/{c}" for c in sorted(self.commands))
        await update.message.reply_text(
            "Just talk to me in plain language — reminders, questions, email, "
            f"calendar, tasks.\nCommands:\n/help\n{cmds}"
        )

    @staticmethod
    async def _echo(text: str) -> str:
        return f"(echo) {text}"

    @staticmethod
    def _reply_payload(reply: Reply, default_channel: str) -> tuple[str, str]:
        if isinstance(reply, OwnerReply):
            return reply.text, reply.history_channel
        return reply, default_channel

    def _record_delivered(self, text: str, channel: str) -> None:
        if self.on_delivered is None:
            return
        try:
            self.on_delivered(text, channel)
        except Exception:
            # Delivery has already succeeded.  Raising now can cause the same
            # Telegram update to be retried and a duplicate visible response.
            log.exception("Telegram reply delivered but history logging failed")
