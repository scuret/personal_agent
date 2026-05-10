"""Audit-log analytics CLI — usage patterns over the conversation archive.

Complements tools/cost_report.py (which is spend-focused) by surfacing
behavioral insights:

  * Activity by hour of day
  * Activity by day of week
  * Sub-agent / tool usage ranking (with avg per-call latency)
  * Slow-turn analysis (turns over a configurable threshold)
  * Conversation-length distribution
  * Reminder + fact + audit row totals

Read-only against data/memory.sqlite. Run from v1/:

    python -m tools.analytics                # last 7 days, default thresh
    python -m tools.analytics --days 30
    python -m tools.analytics --slow-ms 15000
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "memory.sqlite"

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _open() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"audit db not found at {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _bar(count: int, max_count: int, width: int = 30) -> str:
    if max_count <= 0:
        return ""
    filled = int(count / max_count * width)
    return "█" * filled + "·" * (width - filled)


def _print_block(title: str) -> None:
    print()
    print(f"── {title} ".ljust(70, "─"))


# ─── Sections ───────────────────────────────────────────────────────────────


def section_top_overview(conn: sqlite3.Connection, days: int) -> None:
    cutoff = _cutoff(days)
    convs = conn.execute(
        "SELECT COUNT(*) AS c FROM conversations WHERE started_at >= ?", (cutoff,)
    ).fetchone()["c"]
    msgs = conn.execute(
        "SELECT COUNT(*) AS c, "
        "       SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS u, "
        "       SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS a "
        "  FROM messages WHERE created_at >= ?",
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
    reminders_fired = conn.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE fired_at >= ?",
        (cutoff,),
    ).fetchone()["c"]

    _print_block("Overview")
    print(f"  Conversations:        {convs}")
    print(f"  Messages:             {msgs['c']} (user: {msgs['u'] or 0}, assistant: {msgs['a'] or 0})")
    print(f"  Tool calls:           {tool_uses}")
    print(f"  Active facts:         {facts_active}")
    print(f"  Pending reminders:    {reminders_pending}")
    print(f"  Reminders fired:      {reminders_fired} (in window)")


def section_tools(conn: sqlite3.Connection, days: int) -> None:
    cutoff = _cutoff(days)
    rows = conn.execute(
        """SELECT json_extract(payload, '$.name') AS name, COUNT(*) AS n
             FROM api_events
            WHERE kind = 'tool_use' AND timestamp >= ?
         GROUP BY name
         ORDER BY n DESC""",
        (cutoff,),
    ).fetchall()
    if not rows:
        return
    _print_block("Tool usage (count)")
    max_n = rows[0]["n"]
    for r in rows:
        name = r["name"] or "?"
        # Strip the mcp__server__ prefix for display compactness
        short = name.split("__")[-1]
        server = name.split("__")[1] if name.startswith("mcp__") else "?"
        print(f"  {r['n']:>5}  {server:>10} · {short:<35} {_bar(r['n'], max_n, 20)}")


def section_subagents(conn: sqlite3.Connection, days: int) -> None:
    cutoff = _cutoff(days)
    rows = conn.execute(
        """SELECT json_extract(payload, '$.name') AS name, COUNT(*) AS n
             FROM api_events
            WHERE kind = 'tool_use' AND timestamp >= ?
         GROUP BY name""",
        (cutoff,),
    ).fetchall()
    if not rows:
        return
    by_server: Counter[str] = Counter()
    for r in rows:
        name = r["name"] or ""
        if name.startswith("mcp__"):
            parts = name.split("__")
            if len(parts) >= 2:
                by_server[parts[1]] += r["n"]
        else:
            by_server["(builtin)"] += r["n"]
    if not by_server:
        return
    _print_block("Sub-agent usage")
    max_n = max(by_server.values())
    for server, n in by_server.most_common():
        print(f"  {n:>5}  {server:<15} {_bar(n, max_n, 25)}")


def section_hour_of_day(conn: sqlite3.Connection, days: int) -> None:
    cutoff = _cutoff(days)
    rows = conn.execute(
        """SELECT timestamp FROM api_events
            WHERE kind = 'user_input' AND timestamp >= ?""",
        (cutoff,),
    ).fetchall()
    if not rows:
        return
    by_hour: Counter[int] = Counter()
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["timestamp"])
            # Convert to local time using system tz; close enough for analytics.
            local = dt.astimezone()
            by_hour[local.hour] += 1
        except (ValueError, TypeError):
            continue
    if not by_hour:
        return
    max_n = max(by_hour.values())
    _print_block("User messages by hour of day (local)")
    for h in range(24):
        n = by_hour.get(h, 0)
        bar = _bar(n, max_n, 28)
        print(f"  {h:>2}:00  {n:>4}  {bar}")


def section_day_of_week(conn: sqlite3.Connection, days: int) -> None:
    cutoff = _cutoff(days)
    rows = conn.execute(
        """SELECT timestamp FROM api_events
            WHERE kind = 'user_input' AND timestamp >= ?""",
        (cutoff,),
    ).fetchall()
    if not rows:
        return
    by_dow: Counter[int] = Counter()
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["timestamp"]).astimezone()
            by_dow[dt.weekday()] += 1
        except (ValueError, TypeError):
            continue
    if not by_dow:
        return
    max_n = max(by_dow.values())
    _print_block("User messages by day of week (local)")
    for d in range(7):
        n = by_dow.get(d, 0)
        print(f"  {DAY_NAMES[d]}  {n:>4}  {_bar(n, max_n, 28)}")


def section_slow_turns(conn: sqlite3.Connection, days: int, slow_ms: int) -> None:
    cutoff = _cutoff(days)
    rows = conn.execute(
        """SELECT timestamp, conversation_id, metadata
             FROM api_events
            WHERE kind = 'result' AND timestamp >= ?""",
        (cutoff,),
    ).fetchall()
    durations = []
    for r in rows:
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        d = meta.get("duration_ms") or 0
        if d:
            durations.append((d, r["timestamp"], r["conversation_id"]))
    if not durations:
        return
    _print_block("Turn-duration distribution")
    bucket_edges = [0, 1000, 3000, 5000, 10000, 20000, 30000, 60000, 10**9]
    bucket_labels = ["<1s", "1-3s", "3-5s", "5-10s", "10-20s", "20-30s", "30-60s", ">60s"]
    buckets = [0] * (len(bucket_edges) - 1)
    for d, _, _ in durations:
        for i in range(len(bucket_edges) - 1):
            if bucket_edges[i] <= d < bucket_edges[i + 1]:
                buckets[i] += 1
                break
    max_b = max(buckets)
    for i, lbl in enumerate(bucket_labels):
        print(f"  {lbl:>8}  {buckets[i]:>4}  {_bar(buckets[i], max_b, 25)}")

    slow = sorted([d for d in durations if d[0] >= slow_ms], reverse=True)[:5]
    if slow:
        _print_block(f"Slowest 5 turns (over {slow_ms}ms)")
        for d, ts, cid in slow:
            cid_short = (cid or "")[:8]
            print(f"  {d:>6}ms  {ts[:19]}  conv={cid_short}…")


def section_conversation_lengths(conn: sqlite3.Connection, days: int) -> None:
    cutoff = _cutoff(days)
    rows = conn.execute(
        """SELECT c.id, c.source,
                  (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count
             FROM conversations c
            WHERE c.started_at >= ?""",
        (cutoff,),
    ).fetchall()
    if not rows:
        return
    counts = [r["msg_count"] for r in rows if r["msg_count"]]
    if not counts:
        return
    by_source: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r["msg_count"]:
            by_source[r["source"] or "(none)"].append(r["msg_count"])
    _print_block("Conversation lengths (messages)")
    print(f"  Total convs:    {len(counts)}")
    print(f"  Avg messages:   {sum(counts) / len(counts):.1f}")
    print(f"  Median:         {sorted(counts)[len(counts) // 2]}")
    print(f"  Max:            {max(counts)}")
    print()
    print("  By source:")
    for src in sorted(by_source):
        c = by_source[src]
        print(f"    {src:<10}  {len(c):>4} convs, avg {sum(c) / len(c):.1f} msgs")


# ─── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description="Local audit-log analytics.")
    p.add_argument("--days", type=int, default=7, help="Window in days (default 7).")
    p.add_argument(
        "--slow-ms",
        type=int,
        default=15000,
        help="Threshold for the slow-turns table (default 15000ms).",
    )
    args = p.parse_args()

    conn = _open()
    print()
    print(f"=== personal_agent analytics — last {args.days} day(s) ===")

    section_top_overview(conn, args.days)
    section_subagents(conn, args.days)
    section_tools(conn, args.days)
    section_hour_of_day(conn, args.days)
    section_day_of_week(conn, args.days)
    section_slow_turns(conn, args.days, args.slow_ms)
    section_conversation_lengths(conn, args.days)

    print()


if __name__ == "__main__":
    main()
