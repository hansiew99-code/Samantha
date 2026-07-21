"""Nightly consolidation (BRIEF §5): the anti-bloat mechanism.

At 03:00, the day's transcript goes to the Batch API (50% off, Haiku) in one
batch of three requests: extract new facts, refresh the running conversation
summary, and rebuild the capped core-memory block. Memory grows unboundedly on
disk while per-call context stays flat — cost and quality never degrade as
history accumulates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3

import anthropic

from .config import Settings
from .db import kv_get, kv_set
from .governor import Governor, Usage
from .memory import Memory
from .router import HAIKU

log = logging.getLogger(__name__)

CURSOR_KEY = "consolidation.last_message_id"
SUMMARY_KEY = "conversation.summary"
TRANSCRIPT_CLIP = 30_000     # chars of transcript per night (oldest dropped)
CORE_SECTION_CLIP = 2_000    # chars per core section → block stays ≤ ~1.5K tokens
CORE_KEYS = ("identity", "preferences", "current_context")
POLL_SECONDS = 30
MAX_POLLS = 60               # give up after ~30 min

FACTS_PROMPT = """\
You maintain a personal assistant's long-term memory. Extract durable facts \
from the transcript below — preferences, plans, deadlines, relationships, \
habits, biographical details. Skip chit-chat, one-off logistics that already \
resolved, and anything already implied by the known facts listed. Reply with \
ONLY JSON: {"facts": [{"subject": "...", "predicate": "...", "object": "..."}]} \
(use "owner" as subject for the user). Empty list if nothing durable."""

SUMMARY_PROMPT = """\
Summarize the assistant-owner conversation below in at most 80 words, \
focusing on ongoing threads, open loops, and commitments — the things the \
assistant must not lose track of tomorrow. Reply with the summary text only."""

CORE_PROMPT = """\
You maintain the core memory block a personal assistant sees on every single \
request. Given the current block, the highest-value facts, and today's \
summary, produce the updated block. Keep only what earns a place in every \
prompt: who the owner is, stable preferences, and what matters right now. \
Reply with ONLY JSON: {"identity": "...", "preferences": "...", \
"current_context": "..."} — each value plain text, total under 4000 characters."""


class Consolidator:
    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        memory: Memory,
        governor: Governor,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.memory = memory
        self.governor = governor
        self.client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def run(self) -> bool:
        """Returns True if a consolidation batch ran."""
        transcript, last_id = self._collect_transcript()
        if not transcript:
            log.info("consolidation: nothing new to consolidate")
            return False

        requests = self._build_requests(transcript)
        try:
            results = await self._submit(requests)
        except Exception:
            log.exception("consolidation batch failed — will retry tomorrow")
            return False

        if "facts" in results:
            self._apply_facts(results["facts"][0])
        if "summary" in results:
            summary = results["summary"][0].strip()
            if summary:
                kv_set(self.conn, SUMMARY_KEY, summary[:600])
        if "core" in results:
            self._apply_core(results["core"][0])

        for text, usage in results.values():
            self.governor.record(HAIKU, "consolidation", usage, batch=True)

        kv_set(self.conn, CURSOR_KEY, str(last_id))
        log.info("consolidation done through message %d", last_id)
        return True

    # -- gathering -----------------------------------------------------------

    def _collect_transcript(self) -> tuple[str, int]:
        last_id = int(kv_get(self.conn, CURSOR_KEY, "0"))
        rows = self.conn.execute(
            "SELECT id, role, content FROM messages WHERE id > ? ORDER BY id",
            (last_id,),
        ).fetchall()
        if not rows:
            return "", last_id
        lines = [f"{r['role']}: {r['content']}" for r in rows]
        transcript = "\n".join(lines)
        if len(transcript) > TRANSCRIPT_CLIP:
            transcript = transcript[-TRANSCRIPT_CLIP:]
        return transcript, int(rows[-1]["id"])

    def _current_facts_snippet(self, limit: int = 40) -> str:
        rows = self.conn.execute(
            "SELECT subject, predicate, object FROM facts "
            "WHERE superseded_by IS NULL ORDER BY ref_count DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return "\n".join(f"- {r['subject']} {r['predicate']} {r['object']}" for r in rows)

    def _build_requests(self, transcript: str) -> list[dict]:
        known = self._current_facts_snippet()
        summary = kv_get(self.conn, SUMMARY_KEY, "") or "(none)"
        core = self.memory.core_block()

        def req(custom_id: str, system: str, user: str) -> dict:
            return {
                "custom_id": custom_id,
                "params": {
                    "model": HAIKU,
                    "max_tokens": 1500,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            }

        return [
            req("facts", FACTS_PROMPT, f"Known facts:\n{known}\n\nTranscript:\n{transcript}"),
            req("summary", SUMMARY_PROMPT, f"Previous summary: {summary}\n\nTranscript:\n{transcript}"),
            req(
                "core", CORE_PROMPT,
                f"Current core block:\n{core}\n\nTop facts:\n{known}\n\n"
                f"Yesterday's summary: {summary}\n\nToday's transcript (for context):\n{transcript[:8000]}",
            ),
        ]

    # -- batch API (patched out in tests) ------------------------------------

    async def _submit(self, requests: list[dict]) -> dict[str, tuple[str, Usage]]:
        batch = await self.client.messages.batches.create(requests=requests)
        log.info("consolidation batch %s submitted", batch.id)
        for _ in range(MAX_POLLS):
            await asyncio.sleep(POLL_SECONDS)
            b = await self.client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
        else:
            raise TimeoutError(f"batch {batch.id} did not finish in time")

        results: dict[str, tuple[str, Usage]] = {}
        async for entry in await self.client.messages.batches.results(batch.id):
            if entry.result.type != "succeeded":
                log.warning("consolidation request %s: %s", entry.custom_id, entry.result.type)
                continue
            msg = entry.result.message
            text = "".join(b.text for b in msg.content if b.type == "text")
            results[entry.custom_id] = (text, Usage.from_api(msg.usage))
        return results

    # -- applying results ----------------------------------------------------

    def _apply_facts(self, raw: str) -> None:
        parsed = _extract_json(raw)
        count = 0
        for fact in (parsed or {}).get("facts", []):
            subject = str(fact.get("subject", "")).strip()
            predicate = str(fact.get("predicate", "")).strip()
            obj = str(fact.get("object", "")).strip()
            if subject and predicate and obj:
                self.memory.save_fact(subject, predicate, obj, source="consolidation")
                count += 1
        log.info("consolidation: %d fact(s) written", count)

    def _apply_core(self, raw: str) -> None:
        parsed = _extract_json(raw)
        if not parsed:
            log.warning("consolidation: unparseable core block, keeping current")
            return
        for key in CORE_KEYS:
            value = str(parsed.get(key, "")).strip()
            if value:
                self.memory.set_core(key, value[:CORE_SECTION_CLIP])


def _extract_json(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
