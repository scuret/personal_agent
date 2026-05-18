"""Archive query MCP server — aggregate analytics on the agent's own history.

Lets the agent answer "how many" / "which is most used" / "when am I
most active" style questions with direct SQL against `data/memory.sqlite`,
rather than trying to recall heuristically. Complements
`memory_search_conversations` (for finding specific past content) and
`memory_recall_facts` (for looking up stored facts).

Read-only by design — the underlying connection opens with `mode=ro` so
even a tool bug can't mutate the archive.

Tools (namespaced as mcp__archive__<name>):

  archive_activity_summary(days?)
      Counts: conversations, messages, tool calls, active facts,
      reminders. The "what's been going on" overview.

  archive_top_tools(days?, limit?)
      Most-used tools in the window, ranked.

  archive_recent_conversations(days?, limit?)
      List recent conversation rows with their source, message count,
      and last activity time.

  archive_activity_by_hour(days?)
      User messages bucketed by hour of day (local time).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from core.paths import db_path
from memory.store import MemoryStore

DB_PATH = db_path()


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _ro_conn() -> sqlite3.Connection:
    """Read-only connection. Hard belt-and-suspenders against accidental writes."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def create_archive_mcp_server(_store: MemoryStore) -> McpSdkServerConfig:
    """Build the archive query MCP server.

    Takes a MemoryStore parameter for signature-uniformity with the other
    `(store)`-taking factories, even though it doesn't use it (we open
    fresh read-only connections per tool call).
    """

    @tool(
        "archive_activity_summary",
        (
            "Aggregate counts over the agent's own history: conversations, "
            "messages (user/assistant breakdown), tool calls, active facts, "
            "and reminders. Use this when the principal asks 'how much have "
            "we talked' / 'what have we been doing' / similar overview "
            "questions."
        ),
        {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": "Window in days. Default 7.",
                },
            },
            "required": [],
        },
    )
    async def archive_activity_summary(args: dict[str, Any]) -> dict[str, Any]:
        days = int(args.get("days", 7))
        cutoff = _cutoff(days)
        try:
            with _ro_conn() as conn:
                convs = conn.execute(
                    "SELECT COUNT(*) AS c FROM conversations WHERE started_at >= ?",
                    (cutoff,),
                ).fetchone()["c"]
                msgs = conn.execute(
                    """SELECT COUNT(*) AS c,
                              SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS u,
                              SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS a
                         FROM messages WHERE created_at >= ?""",
                    (cutoff,),
                ).fetchone()
                tool_uses = conn.execute(
                    "SELECT COUNT(*) AS c FROM api_events WHERE kind='tool_use' AND timestamp >= ?",
                    (cutoff,),
                ).fetchone()["c"]
                facts_active = conn.execute(
                    "SELECT COUNT(*) AS c FROM facts WHERE is_active = 1"
                ).fetchone()["c"]
                reminders_pending = conn.execute(
                    "SELECT COUNT(*) AS c FROM reminders WHERE fired_at IS NULL AND cancelled_at IS NULL"
                ).fetchone()["c"]
                reminders_fired_window = conn.execute(
                    "SELECT COUNT(*) AS c FROM reminders WHERE fired_at >= ?",
                    (cutoff,),
                ).fetchone()["c"]
        except sqlite3.Error as e:
            return _err(f"archive query failed: {e}")
        return _ok(
            f"Activity summary — last {days} day(s):\n"
            f"  conversations:        {convs}\n"
            f"  messages:             {msgs['c']} "
            f"(user: {msgs['u'] or 0}, assistant: {msgs['a'] or 0})\n"
            f"  tool calls:           {tool_uses}\n"
            f"  active facts (total): {facts_active}\n"
            f"  pending reminders:    {reminders_pending}\n"
            f"  reminders fired:      {reminders_fired_window}"
        )

    @tool(
        "archive_top_tools",
        (
            "Ranked list of which tools the agent has called most often "
            "in the window. Use for 'which integrations am I leaning on' "
            "questions."
        ),
        {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": "Window in days. Default 7.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Max tools to return. Default 15.",
                },
            },
            "required": [],
        },
    )
    async def archive_top_tools(args: dict[str, Any]) -> dict[str, Any]:
        days = int(args.get("days", 7))
        limit = int(args.get("limit", 15))
        cutoff = _cutoff(days)
        try:
            with _ro_conn() as conn:
                rows = conn.execute(
                    """SELECT json_extract(payload, '$.name') AS name, COUNT(*) AS n
                         FROM api_events
                        WHERE kind = 'tool_use' AND timestamp >= ?
                     GROUP BY name
                     ORDER BY n DESC
                        LIMIT ?""",
                    (cutoff, limit),
                ).fetchall()
        except sqlite3.Error as e:
            return _err(f"archive query failed: {e}")
        if not rows:
            return _ok(f"(no tool calls in last {days} day(s))")
        lines = [f"Top tools — last {days} day(s):"]
        for r in rows:
            name = r["name"] or "?"
            short = name.split("__")[-1]
            lines.append(f"  {r['n']:>4}  {short}")
        return _ok("\n".join(lines))

    @tool(
        "archive_recent_conversations",
        (
            "List recent conversation rows with source, message count, and "
            "last-activity time. Use to answer 'when did we last talk' or "
            "'how many threads have we had today'."
        ),
        {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": [],
        },
    )
    async def archive_recent_conversations(args: dict[str, Any]) -> dict[str, Any]:
        days = int(args.get("days", 7))
        limit = int(args.get("limit", 10))
        cutoff = _cutoff(days)
        try:
            with _ro_conn() as conn:
                rows = conn.execute(
                    """SELECT c.id, c.source, c.started_at, c.ended_at,
                              (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count,
                              (SELECT MAX(m.created_at) FROM messages m WHERE m.conversation_id = c.id) AS last_msg
                         FROM conversations c
                        WHERE c.started_at >= ?
                     ORDER BY c.started_at DESC
                        LIMIT ?""",
                    (cutoff, limit),
                ).fetchall()
        except sqlite3.Error as e:
            return _err(f"archive query failed: {e}")
        if not rows:
            return _ok(f"(no conversations in last {days} day(s))")
        lines = [f"Recent conversations — last {days} day(s):"]
        for r in rows:
            cid_short = (r["id"] or "")[:8]
            started = (r["started_at"] or "")[:19]
            last = (r["last_msg"] or "")[:19] if r["last_msg"] else "(empty)"
            lines.append(
                f"  [{cid_short}] {r['source']:<10} started {started} | "
                f"{r['msg_count']} msgs | last {last}"
            )
        return _ok("\n".join(lines))

    @tool(
        "archive_activity_by_hour",
        (
            "Distribution of user messages by hour of day (local time, UTC "
            "stored). Useful for 'when am I most active'. Returns a count "
            "for each of the 24 hours."
        ),
        {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
            },
            "required": [],
        },
    )
    async def archive_activity_by_hour(args: dict[str, Any]) -> dict[str, Any]:
        days = int(args.get("days", 14))
        cutoff = _cutoff(days)
        try:
            with _ro_conn() as conn:
                rows = conn.execute(
                    """SELECT timestamp FROM api_events
                        WHERE kind = 'user_input' AND timestamp >= ?""",
                    (cutoff,),
                ).fetchall()
        except sqlite3.Error as e:
            return _err(f"archive query failed: {e}")
        if not rows:
            return _ok(f"(no user input in last {days} day(s))")
        from collections import Counter
        by_hour: Counter[int] = Counter()
        for r in rows:
            try:
                dt = datetime.fromisoformat(r["timestamp"]).astimezone()
                by_hour[dt.hour] += 1
            except (ValueError, TypeError):
                continue
        max_n = max(by_hour.values()) if by_hour else 1
        bar_width = 24
        lines = [f"User-message activity by hour — last {days} day(s):"]
        for h in range(24):
            n = by_hour.get(h, 0)
            filled = int(n / max_n * bar_width) if max_n else 0
            bar = "█" * filled + "·" * (bar_width - filled)
            lines.append(f"  {h:>2}:00  {n:>3}  {bar}")
        return _ok("\n".join(lines))

    return create_sdk_mcp_server(
        name="archive",
        version="1.0.0",
        tools=[
            archive_activity_summary,
            archive_top_tools,
            archive_recent_conversations,
            archive_activity_by_hour,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "archive_server is in-process; instantiate via create_archive_mcp_server(store) from agent_host."
    )


if __name__ == "__main__":
    main()
