"""Reminders MCP server — schedule one-off pings.

The agent uses this when the principal asks to be reminded about
something at a specific time. Three tools:

  remind(fire_at, message)
      Schedule a reminder. fire_at is ISO 8601 with offset; message is
      what gets sent verbatim (with the OUTGOING_MARKER prefix the
      relay uses for loop prevention) when the time hits.

  list_reminders()
      Show all pending (not-yet-fired, not-cancelled) reminders.

  cancel_reminder(reminder_id)
      Cancel a pending reminder by ID.

The scheduler daemon polls `data/memory.sqlite` on its existing
TICK_SECONDS cadence and fires anything whose fire_at has passed.
Reminders are sent via the same ChatSender the morning brief uses, so
they land in the same iMessage thread.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from memory.store import MemoryStore


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _parse_fire_at(s: str) -> datetime | None:
    """Parse an ISO 8601 datetime, accepting both Z-suffix and +HH:MM offsets."""
    try:
        # Python 3.11+'s fromisoformat handles "Z" too.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def create_reminders_mcp_server(store: MemoryStore) -> McpSdkServerConfig:
    @tool(
        "remind",
        (
            "Schedule a future iMessage reminder. fire_at must be ISO 8601 "
            "with timezone offset (e.g. '2026-05-11T16:00:00-05:00'); the "
            "principal's local timezone is in your system prompt — use it. "
            "If their request is ambiguous (e.g. 'remind me at 3 tomorrow' "
            "with no AM/PM), ask before scheduling.\n\n"
            "The `message` is sent verbatim at fire_at — write it as the "
            "thing the principal will read, in your normal voice. Don't "
            "prefix with 'reminder:' — that adds noise. Examples:\n"
            "  - 'Time to call your mom.'\n"
            "  - 'Laundry should be done — switch it over.'\n"
            "  - 'Wedding anniversary plans were due today; what's the move?'"
        ),
        {
            "type": "object",
            "properties": {
                "fire_at": {
                    "type": "string",
                    "description": "ISO 8601 datetime with offset.",
                },
                "message": {
                    "type": "string",
                    "description": "Verbatim text to send at fire_at.",
                },
            },
            "required": ["fire_at", "message"],
        },
    )
    async def remind(args: dict[str, Any]) -> dict[str, Any]:
        fire_dt = _parse_fire_at(args["fire_at"])
        if fire_dt is None:
            return _err(
                f"couldn't parse fire_at as ISO 8601: {args['fire_at']!r}. "
                "Format: '2026-05-11T16:00:00-05:00'."
            )
        if fire_dt.tzinfo is None:
            return _err(
                f"fire_at must include a timezone offset (got {args['fire_at']!r}). "
                "Use the principal's local offset from your system prompt."
            )
        # Normalize and store as ISO with offset so the scheduler can compare
        # consistently across tz-naive vs tz-aware strings.
        normalized = fire_dt.isoformat()
        rid = store.schedule_reminder(
            fire_at=normalized,
            message=args["message"],
        )
        return _ok(
            f"reminder #{rid} scheduled for {fire_dt.strftime('%Y-%m-%d %H:%M %Z')}: "
            f"{args['message']}"
        )

    @tool(
        "list_reminders",
        "List all pending reminders (not yet fired, not cancelled).",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_reminders(_args: dict[str, Any]) -> dict[str, Any]:
        rems = store.list_pending_reminders(limit=50)
        if not rems:
            return _ok("(no pending reminders)")
        lines = []
        for r in rems:
            fire_dt = _parse_fire_at(r["fire_at"])
            when = fire_dt.strftime("%Y-%m-%d %H:%M %Z") if fire_dt else r["fire_at"]
            lines.append(f"- #{r['id']} @ {when}: {r['message']}")
        return _ok("\n".join(lines))

    @tool(
        "cancel_reminder",
        "Cancel a pending reminder by its ID. Use list_reminders first if you don't know the ID.",
        {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "integer"},
            },
            "required": ["reminder_id"],
        },
    )
    async def cancel_reminder(args: dict[str, Any]) -> dict[str, Any]:
        ok = store.cancel_reminder(int(args["reminder_id"]))
        if ok:
            return _ok(f"cancelled reminder #{args['reminder_id']}")
        return _err(
            f"couldn't cancel #{args['reminder_id']} — either it doesn't "
            "exist, already fired, or was already cancelled."
        )

    return create_sdk_mcp_server(
        name="reminders",
        version="1.0.0",
        tools=[remind, list_reminders, cancel_reminder],
    )


def main() -> None:
    raise NotImplementedError(
        "reminders_server is in-process; instantiate via create_reminders_mcp_server(store) from agent_host."
    )


if __name__ == "__main__":
    main()
