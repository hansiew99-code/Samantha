"""Telegram gateway: long polling (no public URL), strict single-user allowlist.

The gateway is transport only — it hands text to an injected `on_message`
coroutine and button taps to `on_callback`, and exposes `send()` for proactive
pushes. Phase 0 wires an echo handler; the brain replaces it in Phase 1.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)

OnMessage = Callable[[str], Awaitable[str | None]]
OnCallback = Callable[[str], Awaitable[str | None]]


class TelegramGateway:
    def __init__(
        self,
        settings: Settings,
        on_message: OnMessage | None = None,
        on_callback: OnCallback | None = None,
    ) -> None:
        self.settings = settings
        self.on_message = on_message or self._echo
        self.on_callback = on_callback
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
        if self.settings.dry_run or self.app is None:
            log.info("[dry-run] telegram send: %s", text)
            return
        await self.app.bot.send_message(
            chat_id=self.settings.telegram_chat_id, text=text, reply_markup=reply_markup
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
            await update.message.reply_text(reply)

    async def _handle_callback(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or update.callback_query is None:
            return
        query = update.callback_query
        await query.answer()
        if self.on_callback and query.data:
            reply = await self.on_callback(query.data)
            if reply and query.message is not None:
                await query.message.reply_text(reply)

    async def _dispatch_command(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or update.message is None or not update.message.text:
            return
        name = update.message.text.lstrip("/").split()[0].split("@")[0]
        handler = self.commands.get(name)
        if handler:
            reply = await handler(update.message.text)
            if reply:
                await update.message.reply_text(reply)

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
