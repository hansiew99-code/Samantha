"""Gmail: history-API delta polling (near-zero quota) + read/draft/send.

Sending is only ever invoked by the pending-action executor after a Telegram
approval tap — never directly by the model (BRIEF §8).
"""

from __future__ import annotations

import base64
import logging
import sqlite3
from dataclasses import dataclass
from email.message import EmailMessage

from ..db import kv_get, kv_set

log = logging.getLogger(__name__)

HISTORY_KEY = "gmail.history_id"
BODY_CLIP = 1500  # chars per message body injected into context (BRIEF §9)


def extract_body(payload: dict) -> str:
    """Pull text/plain out of a Gmail message payload (recursing into parts)."""
    if payload.get("mimeType", "").startswith("text/plain"):
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        body = extract_body(part)
        if body:
            return body
    return ""


def _headers(msg: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }


@dataclass
class GmailClient:
    creds: object

    def _service(self):
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=self.creds, cache_discovery=False)

    # -- reading -------------------------------------------------------------

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        svc = self._service()
        resp = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        out = []
        for ref in resp.get("messages", []):
            msg = (
                svc.users()
                .messages()
                .get(userId="me", id=ref["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            h = _headers(msg)
            out.append(
                {
                    "id": msg["id"],
                    "thread_id": msg["threadId"],
                    "from": h.get("from", ""),
                    "subject": h.get("subject", ""),
                    "date": h.get("date", ""),
                    "snippet": msg.get("snippet", ""),
                }
            )
        return out

    def read_thread(self, thread_id: str) -> str:
        svc = self._service()
        thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        chunks = []
        for msg in thread.get("messages", []):
            h = _headers(msg)
            body = extract_body(msg.get("payload", {}))[:BODY_CLIP]
            chunks.append(
                f"From: {h.get('from', '?')}\nDate: {h.get('date', '?')}\n"
                f"Subject: {h.get('subject', '?')}\n\n{body or msg.get('snippet', '')}"
            )
        return "\n\n---\n\n".join(chunks)

    # -- delta polling -------------------------------------------------------

    def poll_new(self, conn: sqlite3.Connection) -> list[dict]:
        """New inbox messages since the stored historyId. First run primes the
        cursor and returns nothing (no backfill flood)."""
        svc = self._service()
        history_id = kv_get(conn, HISTORY_KEY)
        if history_id is None:
            profile = svc.users().getProfile(userId="me").execute()
            kv_set(conn, HISTORY_KEY, str(profile["historyId"]))
            return []
        try:
            resp = (
                svc.users()
                .history()
                .list(userId="me", startHistoryId=history_id,
                      historyTypes=["messageAdded"], labelId="INBOX")
                .execute()
            )
        except Exception as exc:  # expired historyId (404) → re-prime
            log.warning("gmail history expired (%s); re-priming", exc)
            profile = svc.users().getProfile(userId="me").execute()
            kv_set(conn, HISTORY_KEY, str(profile["historyId"]))
            return []

        new_ids: list[str] = []
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                labels = added.get("message", {}).get("labelIds", [])
                if "INBOX" in labels and "DRAFT" not in labels and "SENT" not in labels:
                    new_ids.append(added["message"]["id"])
        if "historyId" in resp:
            kv_set(conn, HISTORY_KEY, str(resp["historyId"]))

        events = []
        for mid in dict.fromkeys(new_ids):  # dedupe, keep order
            try:
                msg = (
                    svc.users()
                    .messages()
                    .get(userId="me", id=mid, format="metadata",
                         metadataHeaders=["From", "Subject"])
                    .execute()
                )
            except Exception:
                continue
            h = _headers(msg)
            events.append(
                {
                    "id": mid,
                    "thread_id": msg.get("threadId"),
                    "from": h.get("from", ""),
                    "subject": h.get("subject", ""),
                    "snippet": msg.get("snippet", ""),
                }
            )
        return events

    # -- sending (pending-action executor only) ------------------------------

    def send(self, to: str, subject: str, body: str, thread_id: str | None = None) -> str:
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        sent = self._service().users().messages().send(userId="me", body=payload).execute()
        return sent.get("id", "")
