"""Memory: SQLite is the store of record; the context window only ever sees
a small assembled slice (BRIEF §5).

Facts are never deleted. A contradicting fact supersedes the old row
(`superseded_by`), which drops out of retrieval but stays on disk forever.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from .redaction import redact

_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
_STOPWORDS = frozenset(
    "a an the i me my you your it is are was were be to of in on at for and or "
    "do does did what when where who how can could would should with about".split()
)

_PROVENANCE_STOPWORDS = _STOPWORDS | frozenset(
    "app application source platform chat google gmail email slack clickup calendar "
    "this that these those there here come came coming through land landed sent send "
    "arrive arrived which from did does either vs versus".split()
)
_SOURCE_LABELS = {
    "gchat": "Google Chat",
    "gmail": "Gmail",
    "slack": "Slack",
    "clickup": "ClickUp",
    "calendar": "Google Calendar",
}

# The core block is present on every model request.  Bound both individual
# values and the rendered block so an enthusiastic stream of standing rules or
# a bad migration can never turn long-term memory into an ever-growing prompt.
CORE_VALUE_CHAR_LIMIT = 2_000
CORE_BLOCK_CHAR_LIMIT = 5_800  # ~= 1,450 tokens with the local len/4 estimate


@dataclass
class Fact:
    id: int
    subject: str
    predicate: str
    object: str
    source: str

    def render(self) -> str:
        return f"{self.subject} {self.predicate} {self.object}"


class Memory:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- facts ---------------------------------------------------------------

    def save_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        source: str = "chat",
        *,
        replace_existing: bool = False,
    ) -> int:
        """Insert a durable fact without accidentally erasing multi-value facts.

        ``replace_existing`` is deliberately opt-in.  Predicates such as
        ``likes``, ``works on`` and ``knows`` may have several simultaneous
        values; the previous implementation silently hid every earlier value.
        Use replacement only for genuinely single-valued corrections such as a
        timezone, current employer, or changed deadline.
        """
        subject = subject.strip()
        predicate = predicate.strip()
        obj = obj.strip()
        if not subject or not predicate or not obj:
            raise ValueError("fact subject, predicate, and object must be non-empty")
        existing = self.conn.execute(
            "SELECT id FROM facts WHERE lower(subject) = lower(?) "
            "AND lower(predicate) = lower(?) AND lower(object) = lower(?) "
            "AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
            (subject, predicate, obj),
        ).fetchone()
        if existing is not None:
            existing_id = int(existing["id"])
            if replace_existing:
                self.conn.execute(
                    "UPDATE facts SET superseded_by = ? "
                    "WHERE lower(subject) = lower(?) "
                    "AND lower(predicate) = lower(?) AND id != ? "
                    "AND superseded_by IS NULL",
                    (existing_id, subject, predicate, existing_id),
                )
                self.conn.commit()
            return existing_id

        old_ids: list[int] = []
        if replace_existing:
            cur = self.conn.execute(
                "SELECT id FROM facts WHERE lower(subject) = lower(?) "
                "AND lower(predicate) = lower(?) AND superseded_by IS NULL",
                (subject, predicate),
            )
            old_ids = [int(r["id"]) for r in cur.fetchall()]
        new_id = self.conn.execute(
            "INSERT INTO facts(subject, predicate, object, source) VALUES (?, ?, ?, ?)",
            (subject, predicate, obj, source),
        ).lastrowid
        assert new_id is not None
        for old_id in old_ids:
            self.conn.execute(
                "UPDATE facts SET superseded_by = ? WHERE id = ?", (new_id, old_id)
            )
        self.conn.commit()
        return new_id

    def search_facts(self, query: str, k: int = 8) -> list[Fact]:
        """FTS5 search over current facts only. Bumps ref_count (used by the
        nightly consolidation to rank core-memory candidates)."""
        match = self._fts_query(query)
        if not match:
            return []
        rows = self.conn.execute(
            """SELECT f.id, f.subject, f.predicate, f.object, f.source
               FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
               WHERE facts_fts MATCH ? AND f.superseded_by IS NULL
               ORDER BY rank LIMIT ?""",
            (match, k),
        ).fetchall()
        facts = [Fact(r["id"], r["subject"], r["predicate"], r["object"], r["source"]) for r in rows]
        if facts:
            ids = [f.id for f in facts]
            self.conn.execute(
                f"UPDATE facts SET ref_count = ref_count + 1 "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
            self.conn.commit()
        return facts

    @staticmethod
    def _fts_query(query: str, max_tokens: int = 12) -> str:
        """Sanitize arbitrary user text into an FTS5 OR-query. FTS syntax
        characters in raw text would raise; we only ever pass quoted tokens."""
        tokens = [
            t for t in _TOKEN_RE.findall(query.lower()) if t not in _STOPWORDS and len(t) > 1
        ][:max_tokens]
        return " OR ".join(f'"{t}"' for t in tokens)

    # -- core memory ---------------------------------------------------------

    def core_block(self) -> str:
        """The always-in-context block. Rendered deterministically (sorted by
        key) so identical state produces identical bytes → stable cache.

        The database may retain richer archival state, but this rendered slice
        has a hard size ceiling.  Deterministic rules continue to be enforced
        from the ``rules`` table even if their human-readable echo is clipped.
        """
        rows = self.conn.execute(
            "SELECT key, content FROM core_memory ORDER BY key"
        ).fetchall()
        if not rows:
            return "(core memory is empty — a new relationship. Learn fast.)"
        sections: list[str] = []
        used = 0
        for row in rows:
            section = f"## {row['key']}\n{row['content']}"
            separator = 2 if sections else 0
            remaining = CORE_BLOCK_CHAR_LIMIT - used - separator
            if remaining <= 1:
                break
            if len(section) > remaining:
                section = section[: remaining - 1].rstrip() + "…"
            sections.append(section)
            used += separator + len(section)
            if used >= CORE_BLOCK_CHAR_LIMIT:
                break
        return "\n\n".join(sections)

    def set_core(self, key: str, content: str) -> None:
        content = content.strip()
        if len(content) > CORE_VALUE_CHAR_LIMIT:
            content = content[: CORE_VALUE_CHAR_LIMIT - 1].rstrip() + "…"
        self.conn.execute(
            """INSERT INTO core_memory(key, content, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET content = excluded.content,
                                              updated_at = datetime('now')""",
            (key, content),
        )
        self.conn.commit()

    # -- people --------------------------------------------------------------

    def save_person(self, name: str, **fields: str | int | None) -> int:
        allowed = {"email", "relationship", "timezone", "notes", "vip"}
        row = self.conn.execute(
            "SELECT id FROM people WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if row:
            updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                self.conn.execute(
                    f"UPDATE people SET {sets} WHERE id = ?", (*updates.values(), row["id"])
                )
                self.conn.commit()
            return int(row["id"])
        new_id = self.conn.execute(
            "INSERT INTO people(name, email, relationship, timezone, notes, vip) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                fields.get("email"),
                fields.get("relationship"),
                fields.get("timezone"),
                fields.get("notes"),
                int(bool(fields.get("vip", 0))),
            ),
        ).lastrowid
        self.conn.commit()
        assert new_id is not None
        return new_id

    def search_people(self, query: str, k: int = 4) -> list[sqlite3.Row]:
        like = f"%{query.strip()}%"
        return self.conn.execute(
            "SELECT * FROM people WHERE name LIKE ? OR email LIKE ? OR notes LIKE ? LIMIT ?",
            (like, like, like, k),
        ).fetchall()

    # -- conversation log ----------------------------------------------------

    def log_message(self, role: str, content: str, channel: str = "telegram") -> None:
        # Redact any pasted credential before it becomes durable history that
        # would otherwise be replayed into future prompts and consolidation.
        self.conn.execute(
            "INSERT INTO messages(role, content, channel) VALUES (?, ?, ?)",
            (role, redact(content), channel),
        )
        self.conn.commit()

    def recent_messages(self, limit: int = 20) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT id, role, content, channel FROM messages "
            "WHERE role IN ('user','assistant') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return list(reversed(rows))

    def recent_dialogue_messages(self, limit: int = 20) -> list[sqlite3.Row]:
        """Recent successful dialogue, excluding pushes and degraded replies."""
        rows = self.conn.execute(
            "SELECT id, role, content, channel FROM messages "
            "WHERE role IN ('user','assistant') "
            "AND channel NOT IN ('telegram_push', 'telegram_error') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return list(reversed(rows))

    def recent_proactive_messages(self, limit: int = 2) -> list[sqlite3.Row]:
        """Latest unacknowledged pushes, kept separately from dialogue.

        A push remains the live referent across owner retries and degraded
        responses.  Only a successfully delivered reactive answer/action
        closes it, so one provider failure cannot make the next attempt lose
        the very message the owner was asking about.
        """
        rows = self.conn.execute(
            "SELECT id, role, content, channel FROM messages "
            "WHERE role = 'assistant' AND channel = 'telegram_push' "
            "AND id > COALESCE((SELECT MAX(id) FROM messages "
            "WHERE role = 'assistant' AND channel IN "
            "('telegram_reply', 'telegram_callback', 'telegram_command')), 0) "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return list(reversed(rows))

    def recent_event_source_answer(
        self,
        question: str,
        *,
        limit: int = 200,
    ) -> str | None:
        """Answer a recent event-provenance question locally when unambiguous.

        Source metadata is already durable in ``events_queue``.  Questions
        such as “Chat or Gmail?” should not spend model tokens—or fail merely
        because the language service is unavailable.  Conservative matching
        returns ``None`` on weak or cross-source ties rather than guessing.
        """
        normalized = question.casefold().replace("’", "'")
        mentioned_sources: set[str] = set()
        if re.search(r"\b(?:google\s+)?chat\b", normalized):
            mentioned_sources.add("gchat")
        if re.search(r"\b(?:gmail|e-?mail)\b", normalized):
            mentioned_sources.add("gmail")
        if re.search(r"\bslack\b", normalized):
            mentioned_sources.add("slack")
        if re.search(r"\bclickup\b", normalized):
            mentioned_sources.add("clickup")
        if re.search(r"\bcalendar\b", normalized):
            mentioned_sources.add("calendar")

        asks_alternative = " or " in normalized and len(mentioned_sources) >= 2
        asks_where = bool(
            re.search(r"\b(?:where|which\s+(?:app|source|platform)|what\s+(?:app|source|platform))\b", normalized)
            and re.search(r"\b(?:from|through|on|in|came|landed|sent|arrived)\b", normalized)
        )
        if not asks_alternative and not asks_where:
            return None

        query_tokens = self._provenance_tokens(normalized)
        if len(query_tokens) < 2:
            return None

        rows = self.conn.execute(
            "SELECT id, source, scope, payload, created_at FROM events_queue "
            "WHERE source IN ('gchat','gmail','slack','clickup','calendar') "
            "AND created_at >= datetime('now', '-30 days') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        best_by_source: dict[str, tuple[int, int]] = {}
        for row in rows:
            source = str(row["source"])
            if asks_alternative and source not in mentioned_sources:
                continue
            try:
                payload = json.loads(str(row["payload"]))
            except (TypeError, ValueError):
                payload = str(row["payload"])
            haystack = f"{row['scope']} {json.dumps(payload, ensure_ascii=False, default=str)}"
            hay_tokens = set(self._normalized_tokens(haystack))
            score = sum(token in hay_tokens for token in query_tokens)
            if score < 2:
                continue
            current = best_by_source.get(source)
            candidate = (score, int(row["id"]))
            if current is None or candidate > current:
                best_by_source[source] = candidate

        if not best_by_source:
            return None
        ranked = sorted(
            best_by_source.items(), key=lambda item: item[1], reverse=True
        )
        best_source, (best_score, _event_id) = ranked[0]
        if best_score < max(2, (len(query_tokens) + 1) // 2):
            return None
        if len(ranked) > 1 and ranked[1][1][0] == best_score:
            return None

        label = _SOURCE_LABELS[best_source]
        topic = self._provenance_topic(question)
        if topic:
            return f"{label} — {topic} came through there."
        return f"{label} — that's where it came through."

    @staticmethod
    def _normalized_tokens(value: str) -> list[str]:
        tokens: list[str] = []
        for raw in _TOKEN_RE.findall(value.casefold().replace("’", "'")):
            token = raw[:-2] if raw.endswith("'s") else raw
            if len(token) > 1:
                tokens.append(token)
        return tokens

    @classmethod
    def _provenance_tokens(cls, value: str) -> list[str]:
        return [
            token
            for token in cls._normalized_tokens(value)
            if token not in _PROVENANCE_STOPWORDS
        ][:10]

    @staticmethod
    def _provenance_topic(question: str) -> str | None:
        value = question.strip().rstrip("?.! ")
        patterns = (
            r"^(?P<topic>.+?)\s+(?:is|was)\s+(?:it\s+)?(?:on|in|from)\s+",
            r"^where\s+(?:did|does|was)\s+(?P<topic>.+?)\s+(?:come|came|land|arrive)(?:\s+from)?$",
        )
        for pattern in patterns:
            match = re.match(pattern, value, flags=re.IGNORECASE)
            if not match:
                continue
            topic = " ".join(match.group("topic").split()).strip(" -—:,")
            if topic.casefold() not in {"it", "that", "this"} and len(topic) <= 120:
                return topic
        return None

    def search_messages(self, query: str, k: int = 6) -> list[sqlite3.Row]:
        """Search the durable conversation ledger with a small bounded result.

        This is the same-day safety net before nightly consolidation has turned
        a useful statement into a typed fact.  Raw history stays on disk; only
        short excerpts from the newest matching rows can re-enter context.
        """
        tokens = [
            token
            for token in _TOKEN_RE.findall(query.casefold())
            if token not in _STOPWORDS and len(token) > 1
        ][:8]
        if not tokens:
            return []
        clauses = " OR ".join("lower(content) LIKE ? ESCAPE '\\'" for _ in tokens)
        params = [f"%{self._escape_like(token)}%" for token in tokens]
        return self.conn.execute(
            f"SELECT role, content, channel, created_at FROM messages "
            f"WHERE role IN ('user','assistant') AND ({clauses}) "
            "ORDER BY id DESC LIMIT ?",
            (*params, k),
        ).fetchall()

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
