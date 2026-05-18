"""Cost / usage report from the local audit log.

Reads `data/memory.sqlite` and surfaces:
  - Total Anthropic spend (USD) over the window
  - Token totals (input + output) by model usage
  - Tool-call counts (which integrations the agent leans on)
  - Vision-call totals (separate because they go through the Anthropic
    SDK directly, not through ClaudeSDKClient, so they don't show up in
    the main agent's usage numbers)
  - Daily spend breakdown
  - Top 5 most expensive turns

Run from v1/:
    python -m tools.cost_report               # last 7 days
    python -m tools.cost_report --days 30     # last 30 days
    python -m tools.cost_report --days 1      # today only

This script is read-only — it never modifies the database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.paths import db_path
DB_PATH = db_path()


def _open() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"audit db not found at {DB_PATH} — has the agent ever run?"
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _safe_json(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _agent_summary(conn: sqlite3.Connection, days: int) -> dict[str, Any]:
    """Aggregate `result` events — one per agent turn — for cost + tokens."""
    rows = conn.execute(
        """SELECT timestamp, conversation_id, metadata
             FROM api_events
            WHERE kind = 'result' AND timestamp >= ?
         ORDER BY timestamp""",
        (_cutoff(days),),
    ).fetchall()

    total_cost = 0.0
    in_tok = 0
    out_tok = 0
    by_day: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"cost": 0.0, "turns": 0}
    )
    expensive: list[tuple[float, str, str | None]] = []

    for r in rows:
        meta = _safe_json(r["metadata"]) or {}
        cost = float(meta.get("total_cost_usd") or 0)
        usage = meta.get("usage")
        if isinstance(usage, dict):
            in_tok += int(usage.get("input_tokens") or 0)
            out_tok += int(usage.get("output_tokens") or 0)
        total_cost += cost
        day = r["timestamp"][:10]
        by_day[day]["cost"] = float(by_day[day]["cost"]) + cost
        by_day[day]["turns"] = int(by_day[day]["turns"]) + 1
        expensive.append((cost, r["timestamp"], r["conversation_id"]))

    expensive.sort(reverse=True)
    return {
        "turn_count": len(rows),
        "total_cost": total_cost,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "by_day": dict(by_day),
        "top_expensive": expensive[:5],
    }


def _tool_usage(conn: sqlite3.Connection, days: int) -> Counter[str]:
    rows = conn.execute(
        """SELECT payload FROM api_events
            WHERE kind = 'tool_use' AND timestamp >= ?""",
        (_cutoff(days),),
    ).fetchall()
    counts: Counter[str] = Counter()
    for r in rows:
        payload = _safe_json(r["payload"])
        if isinstance(payload, dict):
            counts[payload.get("name", "?")] += 1
    return counts


def _vision_summary(conn: sqlite3.Connection, days: int) -> dict[str, int]:
    rows = conn.execute(
        """SELECT metadata FROM api_events
            WHERE kind = 'vision_request' AND timestamp >= ?""",
        (_cutoff(days),),
    ).fetchall()
    in_tok = 0
    out_tok = 0
    for r in rows:
        meta = _safe_json(r["metadata"]) or {}
        in_tok += int(meta.get("input_tokens") or 0)
        out_tok += int(meta.get("output_tokens") or 0)
    return {"count": len(rows), "input_tokens": in_tok, "output_tokens": out_tok}


def summary(days: int = 7) -> dict[str, Any]:
    """Public dict-returning summary for the web UI.

    Returns the combined agent/tool/vision shape the CLI report renders.
    `main()` calls this and prints; web routes use the dict directly.
    """
    conn = _open()
    agent = _agent_summary(conn, days)
    tools = _tool_usage(conn, days)
    vision = _vision_summary(conn, days)
    return {
        "days": days,
        "agent": agent,
        "tools_top": tools.most_common(15),
        "tools_total": sum(tools.values()),
        "vision": vision,
    }


def _print_block(title: str) -> None:
    print()
    print(f"── {title} ".ljust(60, "─"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local cost / usage report for personal_agent."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Window in days (default: 7).",
    )
    args = parser.parse_args()

    conn = _open()
    agent = _agent_summary(conn, args.days)
    tools = _tool_usage(conn, args.days)
    vision = _vision_summary(conn, args.days)

    print()
    print(f"=== personal_agent usage report — last {args.days} day(s) ===")

    _print_block("Agent turns")
    print(f"  Turns:           {agent['turn_count']}")
    print(f"  Total cost:      ${agent['total_cost']:.4f}")
    if agent["turn_count"]:
        avg = agent["total_cost"] / agent["turn_count"]
        print(f"  Avg per turn:    ${avg:.4f}")
    print(f"  Input tokens:    {agent['input_tokens']:,}")
    print(f"  Output tokens:   {agent['output_tokens']:,}")

    if vision["count"]:
        _print_block("Vision calls (separate from agent turns)")
        print(f"  Calls:           {vision['count']}")
        print(f"  Input tokens:    {vision['input_tokens']:,}")
        print(f"  Output tokens:   {vision['output_tokens']:,}")

    if tools:
        _print_block("Tool usage (top 15)")
        for name, n in tools.most_common(15):
            print(f"  {n:>5}  {name}")

    if agent["by_day"]:
        _print_block("Daily breakdown")
        for day in sorted(agent["by_day"]):
            d = agent["by_day"][day]
            print(f"  {day}: ${float(d['cost']):.4f}  ({int(d['turns'])} turn(s))")

    if agent["top_expensive"]:
        _print_block("Top 5 most expensive turns")
        for cost, ts, cid in agent["top_expensive"]:
            cid_short = (cid or "")[:8]
            print(f"  ${cost:.4f}  {ts}  conv={cid_short}…")

    print()


if __name__ == "__main__":
    main()
