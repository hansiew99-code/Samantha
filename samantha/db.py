"""SQLite access. One connection, WAL mode, thread-safe via sqlite3's own lock.

All state lives here: memory, reminders, rules, spend, integration cursors.
The context window is never the store of record (BRIEF §1.1).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path


# The daemon intentionally shares one SQLite connection with the Slack listener
# thread and API poll workers.  Serialize the short multi-statement write
# sections so one thread cannot commit another thread's cursor/event handoff.
DB_WRITE_LOCK = threading.RLock()


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _apply_schema(conn)
    if str(db_path) != ":memory:":
        try:
            Path(db_path).chmod(0o600)
        except OSError:
            pass
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    schema = resources.files("samantha").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    # Idempotent migration for databases created before provider-event dedupe.
    event_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(events_queue)").fetchall()
    }
    if "dedupe_key" not in event_columns:
        conn.execute("ALTER TABLE events_queue ADD COLUMN dedupe_key TEXT")
    reminder_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()
    }
    if "delivery_version" not in reminder_columns:
        conn.execute(
            "ALTER TABLE reminders ADD COLUMN delivery_version "
            "INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedupe "
        "ON events_queue(dedupe_key) WHERE dedupe_key IS NOT NULL"
    )
    conn.commit()


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM integration_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(
    conn: sqlite3.Connection, key: str, value: str, *, commit: bool = True
) -> None:
    with DB_WRITE_LOCK:
        conn.execute(
            """INSERT INTO integration_state(key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')""",
            (key, value),
        )
        if commit:
            conn.commit()


def stage_source_event(
    conn: sqlite3.Connection,
    *,
    dedupe_key: str,
    source: str,
    kind: str,
    scope: str,
    payload: dict,
) -> None:
    """Stage an integration event inside the caller's current transaction.

    Pollers use this before advancing their provider cursor.  A crash can then
    leave either both queue row + cursor, or neither—never a skipped event.
    Standing-rule filtering still runs again at sweep/digest time.
    """
    conn.execute(
        "INSERT OR IGNORE INTO events_queue(dedupe_key, source, kind, scope, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (dedupe_key, source, kind, scope, json.dumps(payload)),
    )


def record_sync_success(conn: sqlite3.Connection, source: str) -> None:
    kv_set(conn, f"sync.{source}.last_success", datetime.now(timezone.utc).isoformat())
    kv_set(conn, f"sync.{source}.last_error", "")


def record_sync_failure(conn: sqlite3.Connection, source: str, exc: Exception) -> None:
    # Store only the exception class.  Provider error strings can contain query
    # text or account details and do not belong in durable assistant context.
    kv_set(conn, f"sync.{source}.last_error", type(exc).__name__)


def sync_status(conn: sqlite3.Connection, source: str) -> dict[str, str | None]:
    return {
        "last_success": kv_get(conn, f"sync.{source}.last_success"),
        "last_error": kv_get(conn, f"sync.{source}.last_error") or None,
    }
