"""SQLite-backed memory store.

One file at `data/memory.sqlite` with three table groups:

  CONVERSATION ARCHIVE
    conversations(id, started_at, ended_at, source, metadata)
    messages(conversation_id, role, content, tool_calls, created_at)

  AUDIT LOG (privacy invariant — every API event captured)
    api_events(conversation_id, timestamp, kind, payload, metadata)
      kind ∈ {user_input, assistant_text, tool_use, tool_result, result}

  FACTS
    facts(content, category, tags, confidence, recall stats, is_active)

The store is intentionally process-local: all writes are SQLite WAL mode,
synchronous=NORMAL. Good enough for a single-Mac personal app; revisit
if multiple processes ever need to write concurrently.

Retention: keep everything forever (per project decision). Pruning is
not implemented — add if disk usage becomes an issue.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Resolve the DB path relative to v1/ so the store works from any cwd.
_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "memory.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    source      TEXT NOT NULL,
    metadata    TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    tool_calls      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS api_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    timestamp       TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    metadata        TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON api_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_conv ON api_events(conversation_id);

CREATE TABLE IF NOT EXISTS facts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    content          TEXT NOT NULL,
    category         TEXT NOT NULL,
    tags             TEXT,
    confidence       REAL DEFAULT 1.0,
    created_at       TEXT NOT NULL,
    last_recalled_at TEXT,
    recall_count     INTEGER DEFAULT 0,
    is_active        INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category, is_active);

-- Generic key-value state for daemons (e.g. relay's last-seen ROWID).
CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Scheduled reminders. The agent schedules these via mcp__reminders__remind;
-- the scheduler daemon polls and fires them at fire_at.
CREATE TABLE IF NOT EXISTS reminders (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    fire_at                TEXT NOT NULL,
    message                TEXT NOT NULL,
    source_conversation_id TEXT,
    created_at             TEXT NOT NULL,
    fired_at               TEXT,
    cancelled_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_pending
    ON reminders(fire_at)
    WHERE fired_at IS NULL AND cancelled_at IS NULL;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """Thread-safe SQLite wrapper for the agent's persistent state."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite connections aren't safe to share across threads by default;
        # we create per-thread connections via threading.local instead.
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def _init_schema(self) -> None:
        self._conn().executescript(_SCHEMA)

    # ─── Conversation archive ───────────────────────────────────────────────

    def open_conversation(self, source: str = "cli", metadata: dict[str, Any] | None = None) -> str:
        """Create a new conversation row and return its id."""
        cid = str(uuid.uuid4())
        self._conn().execute(
            "INSERT INTO conversations (id, started_at, source, metadata) VALUES (?, ?, ?, ?)",
            (cid, _now(), source, json.dumps(metadata or {})),
        )
        return cid

    def resume_or_open_conversation(
        self,
        source: str,
        gap_threshold_hours: float = 4.0,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Pick up the most recent same-source conversation if it's still warm.

        "Warm" means the last archived message in that conversation is within
        `gap_threshold_hours`. Otherwise close it (if open) and start a new one.
        Used by the iMessage relay for the 4h-gap rollover rule.
        """
        c = self._conn()
        row = c.execute(
            """SELECT c.id,
                      (SELECT MAX(m.created_at) FROM messages m WHERE m.conversation_id = c.id) AS last_msg
                 FROM conversations c
                WHERE c.source = ? AND c.ended_at IS NULL
             ORDER BY c.started_at DESC
                LIMIT 1""",
            (source,),
        ).fetchone()
        if row:
            # An open conversation exists. Two reuse-cases:
            #   (a) it has messages and the gap is within threshold
            #   (b) it has no messages yet (just opened, never used) — reuse
            #       so we don't accumulate empty conversation rows
            if not row["last_msg"]:
                return str(row["id"])
            last = datetime.fromisoformat(row["last_msg"])
            now = datetime.now(timezone.utc)
            if (now - last).total_seconds() <= gap_threshold_hours * 3600:
                return str(row["id"])
            # Stale — close it and fall through to opening a fresh one.
            c.execute(
                "UPDATE conversations SET ended_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
        return self.open_conversation(source=source, metadata=metadata)

    def close_conversation(self, conversation_id: str) -> None:
        self._conn().execute(
            "UPDATE conversations SET ended_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        self._conn().execute(
            """INSERT INTO messages (conversation_id, role, content, tool_calls, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                conversation_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                _now(),
            ),
        )

    def search_conversations(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Substring search across message content. Returns conversation summaries.

        Naive LIKE search for v1 — adequate for personal-scale data. Can swap
        for FTS5 later if needed.
        """
        rows = self._conn().execute(
            """SELECT c.id, c.started_at, c.source,
                      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
                      (SELECT m.content FROM messages m
                        WHERE m.conversation_id = c.id AND m.content LIKE ?
                        ORDER BY m.created_at LIMIT 1) AS first_match
                 FROM conversations c
                WHERE EXISTS (SELECT 1 FROM messages m
                               WHERE m.conversation_id = c.id AND m.content LIKE ?)
             ORDER BY c.started_at DESC
                LIMIT ?""",
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ─── Audit log ──────────────────────────────────────────────────────────

    def log_api_event(
        self,
        kind: str,
        payload: Any,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record one API event for the privacy audit log.

        `payload` is whatever you want preserved verbatim (str, dict, list).
        It's serialized to JSON. `metadata` is for ancillary info like
        model, token counts, or cost.
        """
        self._conn().execute(
            """INSERT INTO api_events (conversation_id, timestamp, kind, payload, metadata)
               VALUES (?, ?, ?, ?, ?)""",
            (
                conversation_id,
                _now(),
                kind,
                json.dumps(payload, default=str),
                json.dumps(metadata) if metadata else None,
            ),
        )

    # ─── Facts ──────────────────────────────────────────────────────────────

    def log_fact(
        self,
        content: str,
        category: str,
        tags: list[str] | None = None,
        confidence: float = 1.0,
    ) -> int:
        cur = self._conn().execute(
            """INSERT INTO facts (content, category, tags, confidence, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (content, category, json.dumps(tags or []), confidence, _now()),
        )
        # `lastrowid` is set on the cursor returned by execute().
        return int(cur.lastrowid or 0)

    def recall_facts(
        self,
        category: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id, content, category, tags, confidence, created_at FROM facts WHERE is_active = 1"
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if query:
            sql += " AND content LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        results = [dict(r) for r in rows]
        # Bookkeeping: bump recall stats so we know which facts get pulled.
        if results:
            ids = [r["id"] for r in results]
            placeholders = ",".join("?" * len(ids))
            self._conn().execute(
                f"""UPDATE facts SET last_recalled_at = ?, recall_count = recall_count + 1
                    WHERE id IN ({placeholders})""",
                [_now(), *ids],
            )
        # Decode tags JSON for caller convenience.
        for r in results:
            r["tags"] = json.loads(r["tags"]) if r.get("tags") else []
        return results

    def deactivate_fact(self, fact_id: int) -> None:
        """Soft-delete a fact (sets is_active=0)."""
        self._conn().execute("UPDATE facts SET is_active = 0 WHERE id = ?", (fact_id,))

    # ─── Reminders ──────────────────────────────────────────────────────────

    def schedule_reminder(
        self,
        fire_at: str,
        message: str,
        source_conversation_id: str | None = None,
    ) -> int:
        """Insert a pending reminder. Returns the new reminder id."""
        cur = self._conn().execute(
            """INSERT INTO reminders
                   (fire_at, message, source_conversation_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (fire_at, message, source_conversation_id, _now()),
        )
        return int(cur.lastrowid or 0)

    def get_due_reminders(self, before_iso: str) -> list[dict[str, Any]]:
        """Return pending reminders with fire_at <= before_iso, oldest first."""
        rows = self._conn().execute(
            """SELECT id, fire_at, message, source_conversation_id, created_at
                 FROM reminders
                WHERE fired_at IS NULL
                  AND cancelled_at IS NULL
                  AND fire_at <= ?
             ORDER BY fire_at ASC""",
            (before_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_pending_reminders(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return all not-yet-fired, not-cancelled reminders ordered by fire_at."""
        rows = self._conn().execute(
            """SELECT id, fire_at, message, created_at
                 FROM reminders
                WHERE fired_at IS NULL AND cancelled_at IS NULL
             ORDER BY fire_at ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder_fired(self, reminder_id: int) -> None:
        self._conn().execute(
            "UPDATE reminders SET fired_at = ? WHERE id = ?",
            (_now(), reminder_id),
        )

    def cancel_reminder(self, reminder_id: int) -> bool:
        """Cancel a pending reminder. Returns True if it was actually pending."""
        cur = self._conn().execute(
            """UPDATE reminders SET cancelled_at = ?
                WHERE id = ? AND fired_at IS NULL AND cancelled_at IS NULL""",
            (_now(), reminder_id),
        )
        return (cur.rowcount or 0) > 0

    # ─── Generic key-value state ────────────────────────────────────────────

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._conn().execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._conn().execute(
            """INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _now()),
        )
