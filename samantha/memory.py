"""Memory: SQLite is the store of record; the context window only ever sees
a small assembled slice (BRIEF §5).

Facts are never deleted. A contradicting fact supersedes the old row
(`superseded_by`), which drops out of retrieval but stays on disk forever.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
_STOPWORDS = frozenset(
    "a an the i me my you your it is are was were be to of in on at for and or "
    "do does did what when where who how can could would should with about".split()
)


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
        self, subject: str, predicate: str, obj: str, source: str = "chat"
    ) -> int:
        """Insert a fact. An existing *current* fact with the same subject +
        predicate is superseded (kept on disk, excluded from retrieval)."""
        cur = self.conn.execute(
            "SELECT id FROM facts WHERE subject = ? AND predicate = ? AND superseded_by IS NULL",
            (subject, predicate),
        )
        old_ids = [r["id"] for r in cur.fetchall()]
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
        key) so identical state produces identical bytes → stable cache."""
        rows = self.conn.execute(
            "SELECT key, content FROM core_memory ORDER BY key"
        ).fetchall()
        if not rows:
            return "(core memory is empty — a new relationship. Learn fast.)"
        return "\n\n".join(f"## {r['key']}\n{r['content']}" for r in rows)

    def set_core(self, key: str, content: str) -> None:
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
        self.conn.execute(
            "INSERT INTO messages(role, content, channel) VALUES (?, ?, ?)",
            (role, content, channel),
        )
        self.conn.commit()

    def recent_messages(self, limit: int = 20) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE role IN ('user','assistant') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return list(reversed(rows))
