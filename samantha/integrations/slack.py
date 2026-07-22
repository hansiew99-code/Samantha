"""Slack via Socket Mode — outbound websocket, no public URL (BRIEF §2).

Uses slack_sdk's built-in (thread-based) SocketModeClient to avoid an aiohttp
dependency. The listener thread only ever enqueues onto the event bus (SQLite
is WAL + busy-timeout, safe for this write volume); all LLM work happens later
in the asyncio sweep.
"""

from __future__ import annotations

import logging

from ..db import record_sync_failure, record_sync_success
from ..events import EventBus

log = logging.getLogger(__name__)


class SlackService:
    def __init__(self, bot_token: str, app_token: str, bus: EventBus) -> None:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient

        self.web = WebClient(token=bot_token)
        self.socket = SocketModeClient(app_token=app_token, web_client=self.web)
        self.bus = bus
        self.self_user_id: str | None = None

    def start(self) -> None:
        try:
            self.self_user_id = self.web.auth_test().get("user_id")
        except Exception as exc:
            record_sync_failure(self.bus.conn, "slack", exc)
            log.exception("slack auth_test failed — slack disabled")
            return
        if self._on_request not in self.socket.socket_mode_request_listeners:
            self.socket.socket_mode_request_listeners.append(self._on_request)
        try:
            self.socket.connect()
        except Exception as exc:
            record_sync_failure(self.bus.conn, "slack", exc)
            log.exception("slack socket connection failed — continuing without Slack")
            try:
                self.socket.close()
            except Exception:
                log.debug("slack cleanup after failed connect raised", exc_info=True)
            return
        record_sync_success(self.bus.conn, "slack")
        log.info("slack socket mode connected (bot user %s)", self.self_user_id)

    def stop(self) -> None:
        try:
            self.socket.close()
        except Exception:
            log.debug("slack close raised", exc_info=True)

    def post_message(
        self, channel: str, text: str, thread_ts: str | None = None
    ) -> None:
        params = {"channel": channel, "text": text}
        if thread_ts:
            params["thread_ts"] = thread_ts
        self.web.chat_postMessage(**params)

    # -- socket listener (runs on slack_sdk's thread) ------------------------

    def _on_request(self, client, req) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        if req.type != "events_api":
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            return
        event = req.payload.get("event", {})
        etype = event.get("type")
        if event.get("bot_id") or event.get("user") == self.self_user_id:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            return  # never react to ourselves or other bots

        if etype == "app_mention":
            kind, scope = "mention", event.get("channel", "*")
        elif etype == "message" and event.get("channel_type") == "im" and not event.get("subtype"):
            kind, scope = "dm", event.get("user", "*")
        else:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            return

        # Persist before acknowledging the envelope.  If SQLite is unavailable
        # Slack retries; the provider event id makes that retry idempotent.
        self.bus.enqueue(
            "slack",
            kind,
            scope,
            {
                "channel": event.get("channel"),
                "user": event.get("user"),
                "text": (event.get("text") or "")[:500],
                "ts": event.get("ts"),
                "thread_ts": event.get("thread_ts") or event.get("ts"),
            },
            dedupe_key=f"slack:{req.payload.get('event_id') or req.envelope_id}",
        )
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
