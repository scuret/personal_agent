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
import os
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
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

-- Scheduled reminders. The agent schedules these via mcp__reminders__remind
-- (one-off) or mcp__reminders__remind_recurring (recurring); the scheduler
-- daemon polls and fires them at fire_at. Recurring reminders have a JSON
-- recurrence_rule and never get fired_at set — instead, after each fire
-- we advance their fire_at to the next occurrence.
CREATE TABLE IF NOT EXISTS reminders (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    fire_at                TEXT NOT NULL,
    message                TEXT NOT NULL,
    source_conversation_id TEXT,
    created_at             TEXT NOT NULL,
    fired_at               TEXT,
    cancelled_at           TEXT,
    recurrence_rule        TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_pending
    ON reminders(fire_at)
    WHERE fired_at IS NULL AND cancelled_at IS NULL;

-- Per-trigger few-shot examples. The user teaches a trigger
-- (email_triage, morning_brief, weekly_review, …) by recording a
-- positive ("this should have fired") or negative ("this fired but
-- shouldn't") example. At call time, the trigger's prompt assembly
-- (scheduler/trigger_prompts.render_examples_block) reads up to N
-- recent examples per polarity and prepends them as in-context
-- corrections. Old examples soft-delete via is_active=0 — they stay
-- visible in the /learning UI but stop influencing the prompt.
CREATE TABLE IF NOT EXISTS trigger_examples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_name    TEXT NOT NULL,
    polarity        TEXT NOT NULL,        -- 'positive' | 'negative'
    input_payload   TEXT NOT NULL,        -- the raw input the trigger saw
    expected_output TEXT,                 -- nullable; user's stated correct outcome
    note            TEXT,                 -- user's explanation
    created_at      TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_trigger_examples_lookup
    ON trigger_examples(trigger_name, is_active, created_at DESC);
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
        # Audit log + conversation archive + facts all live here. Owner-
        # only file perms. We chmod every time the wrapper is constructed
        # (cheap and idempotent) so an existing world-readable file
        # gets locked down on first run after upgrade. ROADMAP H1.
        for suffix in ("", "-wal", "-shm"):
            companion = self.db_path.with_name(self.db_path.name + suffix)
            if companion.exists():
                try:
                    os.chmod(companion, 0o600)
                except OSError:  # noqa: PERF203 — non-fatal
                    pass

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
        conn = self._conn()
        conn.executescript(_SCHEMA)
        # Defensive migrations — add columns introduced after the table
        # was first created. Older databases may be missing them.
        existing_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(reminders)").fetchall()
        }
        if "recurrence_rule" not in existing_cols:
            conn.execute("ALTER TABLE reminders ADD COLUMN recurrence_rule TEXT")

        # Semantic-recall embeddings. Float32 blobs, dim varies by
        # configured model (768 for bge-base, 384 for bge-small, etc.).
        # NULL is the unmigrated state — backfill_embeddings.py fills
        # historical rows; append_message / log_fact populate new rows.
        msg_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "embedding" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN embedding BLOB")
        # ROADMAP M3 — flag messages authored by someone other than the
        # principal (third parties in opt-in group chats). Default 0 so
        # all existing rows keep their semantics (they're either user-
        # authored or agent-authored). The scheduler runs a daily purge
        # of third-party rows older than group_chat_retention_days.
        if "is_third_party" not in msg_cols:
            conn.execute(
                "ALTER TABLE messages ADD COLUMN is_third_party INTEGER DEFAULT 0"
            )

        fact_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(facts)").fetchall()
        }
        if "embedding" not in fact_cols:
            conn.execute("ALTER TABLE facts ADD COLUMN embedding BLOB")
        # Source tagging — every fact captures the conversation it came
        # from so the /facts review queue can show "where did the agent
        # learn this?" Security batch 5 (F2): with the lockdown that
        # prevents memory_log_fact during automated triggers, facts can
        # only land via interactive turns; this column lets the principal
        # spot facts that came from a chat where they didn't actually
        # ask the agent to remember anything (i.e. the agent following
        # an instruction buried in untrusted content).
        if "source_conversation_id" not in fact_cols:
            conn.execute("ALTER TABLE facts ADD COLUMN source_conversation_id TEXT")
        if "source_message_id" not in fact_cols:
            conn.execute("ALTER TABLE facts ADD COLUMN source_message_id INTEGER")

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
        is_third_party: bool = False,
    ) -> None:
        """Archive one message.

        `is_third_party` is set when the row came from someone other
        than the principal — currently only relevant for iMessage
        group chats opted in via IMESSAGE_GROUP_CHATS. Those rows are
        subject to the group_chat_retention_days purge (ROADMAP M3);
        user-authored and agent-authored rows are kept indefinitely.
        """
        cur = self._conn().execute(
            """INSERT INTO messages
                   (conversation_id, role, content, tool_calls,
                    created_at, is_third_party)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                _now(),
                1 if is_third_party else 0,
            ),
        )
        # Embed the message inline so semantic recall picks it up. If the
        # embedder fails (model missing, OOM, anything), log + continue —
        # the row is still archived, just searchable only via substring.
        row_id = cur.lastrowid
        if row_id and content:
            self._embed_and_update(table="messages", row_id=int(row_id), content=content)

    def purge_api_events(self, older_than_days: int) -> int:
        """Delete `api_events` rows older than `older_than_days`.

        `api_events` is the verbatim audit log of every Claude API
        payload — user messages, assistant replies, tool calls, tool
        results, vision requests/responses. The audit-log retention
        purge is part of ROADMAP H2's fallback path (since SQLCipher
        wheels aren't yet available for arm64 macOS + Python 3.13).

        Returns the row count deleted. 0 = retention disabled.
        Conversations / messages / facts are NEVER touched here —
        those live indefinitely.
        """
        if older_than_days <= 0:
            return 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat()
        cur = self._conn().execute(
            "DELETE FROM api_events WHERE timestamp < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    def purge_third_party_messages(self, older_than_days: int) -> int:
        """Delete is_third_party=1 messages older than `older_than_days`.
        Returns the row count deleted. Called by the scheduler's daily
        purge; safe to call ad-hoc.
        """
        if older_than_days <= 0:
            return 0
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat()
        cur = self._conn().execute(
            "DELETE FROM messages WHERE is_third_party = 1 AND created_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    def search_conversations(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Substring search across message content. Returns conversation summaries.

        Naive LIKE search — kept around as a fallback when the embedder
        is unavailable (e.g. during tests, or first-boot before any
        messages have embeddings). The MCP `memory_search_conversations`
        tool now goes through `semantic_search_conversations` by default.
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

    # ─── Semantic search (vector + LIKE re-rank) ────────────────────────────
    #
    # Both methods score each row by `cosine(query, row.embedding) + boost`,
    # where boost = SEMANTIC_LITERAL_BOOST (0.10) iff the query string
    # appears in the row's content (case-insensitive substring).
    # Embeddings are normalized at encode time so cosine == dot product.

    SEMANTIC_LITERAL_BOOST: float = 0.10
    SEMANTIC_SEARCH_FLOOR: float = 0.30  # below this, we won't surface a row at all

    def _embed_query(self, query: str):
        """Encode a query string. Returns the numpy float32 vector or
        None if the embedder is unavailable — callers fall back to LIKE."""
        try:
            from memory.embedder import encode  # lazy import
            import numpy as np

            vec = encode(query)
            return np.asarray(vec, dtype=np.float32)
        except Exception as e:  # noqa: BLE001
            print(f"[memory] semantic search embed failed: {e}", file=sys.stderr)
            return None

    def semantic_search_conversations(
        self,
        query: str,
        limit: int = 10,
        hybrid: bool = True,
    ) -> list[dict[str, Any]]:
        """Vector + (optional) substring re-rank over the messages table.

        Returns one row per conversation (the conversation containing the
        best-scoring message) in the same shape as `search_conversations`:
            {id, started_at, source, message_count, first_match}
        Falls back to substring search if the embedder isn't available.
        """
        q_vec = self._embed_query(query)
        if q_vec is None:
            return self.search_conversations(query, limit=limit)

        import numpy as np

        rows = self._conn().execute(
            """SELECT m.id        AS msg_id,
                      m.conversation_id,
                      m.content,
                      m.created_at,
                      m.embedding
                 FROM messages m
                WHERE m.embedding IS NOT NULL"""
        ).fetchall()
        if not rows:
            # No embeddings yet (fresh install pre-backfill) — fall back.
            return self.search_conversations(query, limit=limit)

        embeddings = np.stack(
            [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
        )
        sims = embeddings @ q_vec  # both sides normalized → cosine

        q_lower = query.lower()
        boosts = np.array(
            [
                self.SEMANTIC_LITERAL_BOOST if q_lower in (r["content"] or "").lower() else 0.0
                for r in rows
            ],
            dtype=np.float32,
        ) if hybrid else np.zeros(len(rows), dtype=np.float32)
        scores = sims + boosts

        # Walk message hits in score order, dedup by conversation_id,
        # stop after `limit` distinct conversations.
        order = np.argsort(-scores)
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for idx in order:
            score = float(scores[idx])
            if score < self.SEMANTIC_SEARCH_FLOOR:
                break
            r = rows[idx]
            conv_id = r["conversation_id"]
            if conv_id in seen:
                continue
            seen.add(conv_id)

            # Fetch the conversation summary the MCP tool expects.
            # Alias `conversations` as `c` so the subquery's `c.id` is
            # unambiguous (a bare `id` would resolve to messages.id
            # under SQLite's column-resolution rules).
            conv = self._conn().execute(
                """SELECT c.id, c.started_at, c.source,
                          (SELECT COUNT(*) FROM messages mm WHERE mm.conversation_id = c.id) AS message_count
                     FROM conversations c WHERE c.id = ?""",
                (conv_id,),
            ).fetchone()
            if not conv:
                continue
            out.append({
                "id": conv["id"],
                "started_at": conv["started_at"],
                "source": conv["source"],
                "message_count": conv["message_count"],
                "first_match": r["content"],
                "score": round(score, 4),
            })
            if len(out) >= limit:
                break
        return out

    def semantic_recall_facts(
        self,
        query: str,
        category: str | None = None,
        limit: int = 20,
        hybrid: bool = True,
    ) -> list[dict[str, Any]]:
        """Vector + (optional) substring re-rank over the active facts table.

        Same shape as `recall_facts` so the MCP tool surface is
        unchanged. Falls back to SQL substring filtering if the embedder
        isn't available.
        """
        q_vec = self._embed_query(query)
        if q_vec is None:
            return self.recall_facts(category=category, query=query, limit=limit)

        import numpy as np

        sql = (
            "SELECT id, content, category, tags, confidence, created_at, embedding "
            "FROM facts WHERE is_active = 1 AND embedding IS NOT NULL"
        )
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        rows = self._conn().execute(sql, params).fetchall()
        if not rows:
            return self.recall_facts(category=category, query=query, limit=limit)

        embeddings = np.stack(
            [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
        )
        sims = embeddings @ q_vec

        q_lower = query.lower()
        boosts = np.array(
            [
                self.SEMANTIC_LITERAL_BOOST if q_lower in (r["content"] or "").lower() else 0.0
                for r in rows
            ],
            dtype=np.float32,
        ) if hybrid else np.zeros(len(rows), dtype=np.float32)
        scores = sims + boosts

        order = np.argsort(-scores)[:limit]
        results: list[dict[str, Any]] = []
        for idx in order:
            score = float(scores[idx])
            if score < self.SEMANTIC_SEARCH_FLOOR:
                break
            r = rows[idx]
            results.append({
                "id": r["id"],
                "content": r["content"],
                "category": r["category"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "confidence": r["confidence"],
                "created_at": r["created_at"],
                "score": round(score, 4),
            })

        # Bookkeeping: bump recall stats so we know which facts get pulled.
        if results:
            ids = [r["id"] for r in results]
            placeholders = ",".join("?" * len(ids))
            self._conn().execute(
                f"""UPDATE facts SET last_recalled_at = ?, recall_count = recall_count + 1
                    WHERE id IN ({placeholders})""",
                [_now(), *ids],
            )
        return results

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
        source_conversation_id: str | None = None,
        source_message_id: int | None = None,
    ) -> int:
        cur = self._conn().execute(
            """INSERT INTO facts
                   (content, category, tags, confidence, created_at,
                    source_conversation_id, source_message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                content,
                category,
                json.dumps(tags or []),
                confidence,
                _now(),
                source_conversation_id,
                source_message_id,
            ),
        )
        # `lastrowid` is set on the cursor returned by execute().
        fact_id = int(cur.lastrowid or 0)
        if fact_id and content:
            self._embed_and_update(table="facts", row_id=fact_id, content=content)
        return fact_id

    # ─── Embeddings ─────────────────────────────────────────────────────────

    def _embed_and_update(self, table: str, row_id: int, content: str) -> None:
        """Encode `content` and persist the bytes into <table>.embedding.

        Failure is non-fatal — the row stays archived without a vector,
        and substring search still finds it. Errors print to stderr so
        the daemon log surfaces them, but `append_message` / `log_fact`
        callers don't see exceptions from this path.
        """
        try:
            from memory.embedder import encode_to_bytes  # lazy: keeps daemon startup snappy

            blob = encode_to_bytes(content)
        except Exception as e:  # noqa: BLE001
            print(f"[memory] embed failed for {table}#{row_id}: {e}", file=sys.stderr)
            return
        try:
            self._conn().execute(
                f"UPDATE {table} SET embedding = ? WHERE id = ?",
                (blob, row_id),
            )
        except sqlite3.Error as e:
            print(f"[memory] embed write failed for {table}#{row_id}: {e}", file=sys.stderr)

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

    def recent_facts(self, hours: int = 24, limit: int = 100) -> list[dict[str, Any]]:
        """Facts created in the last `hours` hours — for the /facts review
        queue. Newest first. Includes inactive rows so the principal can
        see what was just deactivated alongside what was just added.

        Security batch 5 (F2): the review queue is the human-in-the-loop
        check for facts the agent logged in response to chat content that
        might have been prompt-injection-influenced. The source_conversation_id
        column lets the UI link back to the chat where the fact was learned.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=int(hours))
        ).isoformat()
        rows = self._conn().execute(
            """SELECT id, content, category, tags, confidence, created_at,
                      source_conversation_id, source_message_id, is_active
                 FROM facts
                WHERE created_at >= ?
             ORDER BY created_at DESC
                LIMIT ?""",
            (cutoff, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def deactivate_fact(self, fact_id: int) -> None:
        """Soft-delete a fact (sets is_active=0)."""
        self._conn().execute("UPDATE facts SET is_active = 0 WHERE id = ?", (fact_id,))

    # ─── Reminders ──────────────────────────────────────────────────────────

    def schedule_reminder(
        self,
        fire_at: str,
        message: str,
        source_conversation_id: str | None = None,
        recurrence_rule: dict[str, Any] | None = None,
    ) -> int:
        """Insert a pending reminder. Returns the new reminder id.

        If `recurrence_rule` is provided, the reminder fires repeatedly:
        each time the scheduler delivers it, its fire_at advances to the
        next occurrence (and fired_at is never set). Cancel via cancel_reminder.
        """
        cur = self._conn().execute(
            """INSERT INTO reminders
                   (fire_at, message, source_conversation_id, created_at, recurrence_rule)
               VALUES (?, ?, ?, ?, ?)""",
            (
                fire_at,
                message,
                source_conversation_id,
                _now(),
                json.dumps(recurrence_rule) if recurrence_rule else None,
            ),
        )
        return int(cur.lastrowid or 0)

    def get_due_reminders(self, before_iso: str) -> list[dict[str, Any]]:
        """Return pending reminders with fire_at <= before_iso, oldest first.

        Includes both one-off and recurring reminders. The scheduler
        differentiates by checking `recurrence_rule`.
        """
        rows = self._conn().execute(
            """SELECT id, fire_at, message, source_conversation_id, created_at, recurrence_rule
                 FROM reminders
                WHERE fired_at IS NULL
                  AND cancelled_at IS NULL
                  AND fire_at <= ?
             ORDER BY fire_at ASC""",
            (before_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_pending_reminders(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return all not-yet-fired, not-cancelled reminders ordered by fire_at.

        Includes recurring reminders (which have non-null recurrence_rule
        and never get fired_at set).
        """
        rows = self._conn().execute(
            """SELECT id, fire_at, message, created_at, recurrence_rule
                 FROM reminders
                WHERE fired_at IS NULL AND cancelled_at IS NULL
             ORDER BY fire_at ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder_fired(self, reminder_id: int) -> None:
        """One-off completion. Don't call for recurring reminders — use
        advance_reminder_fire_at instead so they keep firing."""
        self._conn().execute(
            "UPDATE reminders SET fired_at = ? WHERE id = ?",
            (_now(), reminder_id),
        )

    def advance_reminder_fire_at(self, reminder_id: int, next_fire_at: str) -> None:
        """For recurring reminders: roll fire_at forward to the next occurrence
        without marking fired_at. Reminder stays pending."""
        self._conn().execute(
            "UPDATE reminders SET fire_at = ? WHERE id = ?",
            (next_fire_at, reminder_id),
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

    # ─── Per-trigger learning examples ──────────────────────────────────────

    # Allowed polarities + a trigger-name allowlist. Kept here (not in the
    # MCP server) so anywhere in the codebase that touches trigger_examples
    # validates against the same set.
    TRIGGER_EXAMPLE_POLARITIES = ("positive", "negative")
    TRIGGER_NAMES = (
        "email_triage",
        "morning_brief",
        "weekly_review",
        # Phase 2 (LLM gating not yet wired):
        "delivery_watch",
        "expected_arrivals",
    )

    def record_trigger_example(
        self,
        trigger_name: str,
        polarity: str,
        input_payload: str,
        expected_output: str | None = None,
        note: str | None = None,
    ) -> int:
        """Persist one user correction for a trigger. Returns the new row id.

        Polarity meaning:
          'positive' = "this input SHOULD have fired the trigger" / "this
                       output was correct" — bias the next call toward
                       firing / producing similar output.
          'negative' = "this input fired but shouldn't have" / "this output
                       was wrong" — bias the next call away from it.
        """
        if polarity not in self.TRIGGER_EXAMPLE_POLARITIES:
            raise ValueError(
                f"polarity must be one of {self.TRIGGER_EXAMPLE_POLARITIES}, "
                f"got {polarity!r}"
            )
        if trigger_name not in self.TRIGGER_NAMES:
            raise ValueError(
                f"trigger_name must be one of {self.TRIGGER_NAMES}, "
                f"got {trigger_name!r}"
            )
        cur = self._conn().execute(
            """INSERT INTO trigger_examples
                   (trigger_name, polarity, input_payload, expected_output,
                    note, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                trigger_name,
                polarity,
                input_payload,
                expected_output,
                note,
                _now(),
            ),
        )
        return int(cur.lastrowid or 0)

    def list_trigger_examples(
        self,
        trigger_name: str | None = None,
        polarity: str | None = None,
        limit: int = 20,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Return examples for inspection / injection.

        Newest first. Pass active_only=False to include soft-deleted rows
        (used by the /learning UI's 'archived' view).
        """
        where: list[str] = []
        params: list[Any] = []
        if trigger_name:
            where.append("trigger_name = ?")
            params.append(trigger_name)
        if polarity:
            where.append("polarity = ?")
            params.append(polarity)
        if active_only:
            where.append("is_active = 1")
        sql = "SELECT * FROM trigger_examples"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def soft_delete_trigger_example(self, example_id: int) -> bool:
        """Mark one example inactive. Returns True if a row was updated."""
        cur = self._conn().execute(
            "UPDATE trigger_examples SET is_active = 0 WHERE id = ? AND is_active = 1",
            (int(example_id),),
        )
        return (cur.rowcount or 0) > 0

    def count_trigger_examples(
        self, trigger_name: str, active_only: bool = True
    ) -> int:
        """Used by /learning UI for the per-trigger badge."""
        sql = "SELECT COUNT(*) AS c FROM trigger_examples WHERE trigger_name = ?"
        params: list[Any] = [trigger_name]
        if active_only:
            sql += " AND is_active = 1"
        row = self._conn().execute(sql, params).fetchone()
        return int(row["c"] or 0)
