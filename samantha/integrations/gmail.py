"""Gmail: history-API delta polling (near-zero quota) + read/draft/send.

Sending is only ever invoked by the pending-action executor after a Telegram
approval tap — never directly by the model (BRIEF §8).
"""

from __future__ import annotations

import base64
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage

from ..db import DB_WRITE_LOCK, kv_get, kv_set, stage_source_event

log = logging.getLogger(__name__)

HISTORY_KEY = "gmail.history_id"
INGESTION_CHECKPOINT_KEY = "gmail.ingest.last_success"
BODY_CLIP = 1500  # chars per message body injected into context (BRIEF §9)
THREAD_RESULT_CLIP = 3_800


class PartialGmailReadError(RuntimeError):
    """Gmail returned only a best-effort subset; its cursor was not advanced."""


def _http_status(exc: Exception) -> int | None:
    """Best-effort status extraction without coupling tests to googleapiclient."""
    response = getattr(exc, "resp", None)
    raw = getattr(response, "status", None) or getattr(exc, "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


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
    last_poll_complete: bool = field(default=True, init=False)

    def _service(self):
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=self.creds, cache_discovery=False)

    # -- reading -------------------------------------------------------------

    def search(self, query: str, max_results: int | None = 10) -> list[dict]:
        svc = self._service()
        refs: list[dict] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while max_results is None or len(refs) < max_results:
            page_size = 500 if max_results is None else min(500, max_results - len(refs))
            params: dict = {"userId": "me", "q": query, "maxResults": page_size}
            if page_token:
                params["pageToken"] = page_token
            resp = svc.users().messages().list(**params).execute()
            refs.extend(resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            if page_token in seen_tokens:
                raise RuntimeError("Gmail search pagination token repeated")
            seen_tokens.add(page_token)
        if max_results is not None:
            refs = refs[:max_results]
        out = []
        for ref in refs:
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
                    "internal_date": msg.get("internalDate", ""),
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
                f"From: {str(h.get('from') or '?')[:240]}\n"
                f"Date: {str(h.get('date') or '?')[:120]}\n"
                f"Subject: {str(h.get('subject') or '?')[:240]}\n\n"
                f"{body or str(msg.get('snippet', ''))[:BODY_CLIP]}"
            )
        full = "\n\n---\n\n".join(chunks)
        if len(full) <= THREAD_RESULT_CLIP:
            return full
        # Preserve the latest question/reply, not merely the oldest prefix that
        # a generic result truncator would keep. Add as much recent context as
        # fits, plus a short opening excerpt when older history was omitted.
        selected: list[str] = []
        used = 0
        recent_budget = THREAD_RESULT_CLIP - 700
        for chunk in reversed(chunks):
            cost = len(chunk) + (7 if selected else 0)
            if selected and used + cost > recent_budget:
                break
            selected.append(chunk[:recent_budget] if not selected else chunk)
            used += min(cost, recent_budget)
        selected.reverse()
        rendered_recent = "\n\n---\n\n".join(selected)
        if len(selected) < len(chunks):
            opening = chunks[0][:500].rstrip()
            return (
                opening
                + "\n\n[older middle messages omitted]\n\n"
                + rendered_recent
            )[:THREAD_RESULT_CLIP]
        return rendered_recent[:THREAD_RESULT_CLIP]

    # -- delta polling -------------------------------------------------------

    def poll_new(self, conn: sqlite3.Connection) -> list[dict]:
        """New inbox messages since the stored historyId. First run primes the
        cursor and returns nothing (no backfill flood)."""
        self.last_poll_complete = True
        svc = self._service()
        history_id = kv_get(conn, HISTORY_KEY)
        if history_id is None:
            try:
                profile = svc.users().getProfile(userId="me").execute()
            except Exception:
                self.last_poll_complete = False
                raise
            kv_set(conn, HISTORY_KEY, str(profile["historyId"]))
            return []
        # Page through the full history. Only advance the cursor after every
        # page has been read: if any page fails mid-way we discard the partial
        # result and retry from the same cursor next cycle, so a busy inbox
        # never loses messages and never double-advances past unread history.
        new_ids: list[str] = []
        latest_history_id: str | None = None
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        while True:
            params: dict = dict(
                userId="me", startHistoryId=history_id,
                historyTypes=["messageAdded"], labelId="INBOX",
            )
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = svc.users().history().list(**params).execute()
            except Exception as exc:
                if page_token is None and _http_status(exc) == 404:
                    # Gmail only signals an expired/out-of-range history cursor
                    # with 404.  A network/5xx/auth error must keep the cursor;
                    # the old code re-primed on *every* exception and could skip
                    # mail permanently during a transient outage.
                    log.warning("gmail history expired; reconciling before re-prime")
                    last_success = kv_get(conn, INGESTION_CHECKPOINT_KEY)
                    backfill: list[dict] = []
                    try:
                        # Snapshot the new history cursor *before* searching.
                        # Mail arriving during/after reconciliation will then
                        # remain newer than this cursor and be picked up by the
                        # next delta poll instead of being skipped in a race.
                        profile = svc.users().getProfile(userId="me").execute()
                        if last_success:
                            since = datetime.fromisoformat(last_success)
                            query = f"in:inbox after:{int(since.timestamp())}"
                        else:
                            # Upgrade-safe fallback: generic read freshness can
                            # come from a bounded digest search, so it is never
                            # a valid ingestion boundary. With no dedicated
                            # checkpoint, reconcile the whole inbox rather than
                            # silently skip mail.
                            query = "in:inbox"
                        backfill = self.search(query, max_results=None)
                    except Exception:
                        self.last_poll_complete = False
                        log.warning(
                            "gmail history reconciliation failed; keeping old cursor",
                            exc_info=True,
                        )
                        return []
                    with DB_WRITE_LOCK:
                        try:
                            self._stage_events(conn, backfill)
                            kv_set(
                                conn,
                                HISTORY_KEY,
                                str(profile["historyId"]),
                                commit=False,
                            )
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            self.last_poll_complete = False
                            raise
                    return backfill
                self.last_poll_complete = False
                if page_token is None:
                    log.warning(
                        "gmail history fetch failed; keeping cursor for retry",
                        exc_info=True,
                    )
                else:  # mid-pagination failure → discard, retry same cursor next run
                    log.warning("gmail history page fetch failed; retrying next cycle", exc_info=True)
                return []
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    labels = added.get("message", {}).get("labelIds", [])
                    if "INBOX" in labels and "DRAFT" not in labels and "SENT" not in labels:
                        new_ids.append(added["message"]["id"])
            latest_history_id = resp.get("historyId", latest_history_id)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            if page_token in seen_page_tokens:
                self.last_poll_complete = False
                log.warning("gmail history pagination token repeated; keeping cursor")
                return []
            seen_page_tokens.add(page_token)
        events = []
        metadata_failed = False
        for mid in dict.fromkeys(new_ids):  # dedupe, keep order
            try:
                msg = (
                    svc.users()
                    .messages()
                    .get(userId="me", id=mid, format="metadata",
                         metadataHeaders=["From", "Subject"])
                    .execute()
                )
            except Exception as exc:
                if _http_status(exc) == 404:
                    # A message can be deleted between history.list and
                    # messages.get. It is permanently unavailable, not a
                    # retryable reason to wedge the mailbox cursor forever.
                    log.info("gmail message %s disappeared before metadata read", mid)
                    continue
                metadata_failed = True
                break
            h = _headers(msg)
            events.append(
                {
                    "id": mid,
                    "thread_id": msg.get("threadId"),
                    "from": h.get("from", ""),
                    "subject": h.get("subject", ""),
                    "internal_date": msg.get("internalDate", ""),
                    "snippet": msg.get("snippet", ""),
                }
            )
        if metadata_failed:
            # Do not advance past a message whose metadata we failed to fetch.
            # The next poll safely replays the same history window.
            self.last_poll_complete = False
            log.warning("gmail metadata fetch failed; keeping history cursor for retry")
            with DB_WRITE_LOCK:
                try:
                    self._stage_events(conn, events)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return events
        with DB_WRITE_LOCK:
            try:
                self._stage_events(conn, events)
                if latest_history_id:
                    kv_set(conn, HISTORY_KEY, str(latest_history_id), commit=False)
                conn.commit()
            except Exception:
                conn.rollback()
                self.last_poll_complete = False
                raise
        return events

    @staticmethod
    def _stage_events(conn: sqlite3.Connection, events: list[dict]) -> None:
        for event in events:
            message_id = str(event.get("id") or "")
            if not message_id:
                continue
            stage_source_event(
                conn,
                dedupe_key=f"gmail:{message_id}",
                source="gmail",
                kind="new_email",
                scope=str(event.get("from") or "*"),
                payload=event,
            )

    def matches_watch(self, criteria: dict) -> dict | None:
        """Live reconciliation for a conditional email watcher.

        Gmail search narrows the candidate set; local checks remain authoritative
        so model-provided names cannot accidentally broaden a match.
        """
        since_raw = str(criteria.get("since", ""))
        since = datetime.fromisoformat(since_raw) if since_raw else None
        parts: list[str] = []
        if since is not None:
            parts.append(f"after:{int(since.timestamp())}")
        sender = str(criteria.get("sender_contains", "")).replace('"', "").strip()
        subject = str(criteria.get("subject_contains", "")).replace('"', "").strip()
        if sender:
            parts.append(f'from:"{sender}"')
        if subject:
            parts.append(f'subject:"{subject}"')
        for msg in self.search(" ".join(parts), max_results=20):
            if sender and sender.casefold() not in str(msg.get("from", "")).casefold():
                continue
            if subject and subject.casefold() not in str(msg.get("subject", "")).casefold():
                continue
            if since is not None:
                try:
                    arrived_ms = int(msg.get("internal_date", ""))
                except (TypeError, ValueError):
                    continue
                if arrived_ms / 1000 <= since.timestamp():
                    continue
            return msg
        return None

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
