"""Reminders MCP server — one-off + recurring schedules.

Four tools:

  remind(fire_at, message)
      Schedule a one-off reminder. fire_at is ISO 8601 with offset.

  remind_recurring(message, time_of_day, schedule_type, ...)
      Schedule a repeating reminder that fires daily / weekdays / weekly /
      monthly. After each fire, the scheduler rolls fire_at to the next
      occurrence so it keeps going until cancelled.

  list_reminders()
      Show all pending (not-yet-fired, not-cancelled) reminders, both
      one-off and recurring.

  cancel_reminder(reminder_id)
      Cancel either kind by ID.

The scheduler daemon polls `data/memory.sqlite` on its existing
TICK_SECONDS cadence and fires anything whose fire_at has passed.
Reminders are sent via the same transport-aware sender the morning
brief uses (iMessage or Telegram, depending on RELAY_TRANSPORT).
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import Any

import pytz
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from memory.store import MemoryStore

# Schedule type values accepted by remind_recurring.
SCHEDULE_TYPES = ("daily", "weekdays", "weekly", "monthly")

# Weekday names (lowercase) → Python weekday() integer (Mon=0, Sun=6).
WEEKDAY_LOOKUP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


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


def _parse_hhmm(s: str) -> dtime | None:
    """Parse 'HH:MM' (24-hour). Returns None on bad input."""
    try:
        h_str, m_str = s.strip().split(":")
        h, m = int(h_str), int(m_str)
        if 0 <= h < 24 and 0 <= m < 60:
            return dtime(h, m)
    except (ValueError, AttributeError):
        pass
    return None


def _user_tz_name() -> str:
    import os
    return os.environ.get("USER_TIMEZONE", "America/Chicago")


def _next_recurrence(rule: dict[str, Any], reference: datetime) -> datetime | None:
    """Compute the next fire_at AFTER the reference moment, given a recurrence rule.

    Rule shape (validated upstream when scheduling):
      { schedule_type: 'daily' | 'weekdays' | 'weekly' | 'monthly',
        time_of_day: 'HH:MM',
        weekday: int (0=Mon..6=Sun)            — for 'weekly'
        day_of_month: int (1..31)              — for 'monthly'
        tz: 'America/Chicago' (etc.) }

    For 'monthly', if the target month has fewer days than day_of_month
    (e.g. day_of_month=31 in February), we fire on the last day instead
    so the user doesn't silently miss months.
    """
    schedule_type = rule.get("schedule_type")
    fire_time = _parse_hhmm(rule.get("time_of_day", ""))
    if fire_time is None or schedule_type not in SCHEDULE_TYPES:
        return None
    try:
        tz = pytz.timezone(rule.get("tz") or _user_tz_name())
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.timezone(_user_tz_name())

    ref_local = reference.astimezone(tz)

    def at_time_on(date_obj: datetime) -> datetime:
        # `date_obj` may be naive or aware; we use only its date components.
        return tz.localize(
            datetime(date_obj.year, date_obj.month, date_obj.day, fire_time.hour, fire_time.minute)
        ) if date_obj.tzinfo is None else date_obj.replace(
            hour=fire_time.hour, minute=fire_time.minute, second=0, microsecond=0
        )

    if schedule_type == "daily":
        candidate = at_time_on(ref_local)
        while candidate <= reference:
            candidate += timedelta(days=1)
        return candidate

    if schedule_type == "weekdays":  # Mon-Fri
        candidate = at_time_on(ref_local)
        while candidate <= reference or candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate

    if schedule_type == "weekly":
        target_wd = int(rule.get("weekday", 0))
        candidate = at_time_on(ref_local)
        # Advance to the target weekday, then ensure it's strictly after reference.
        days_ahead = (target_wd - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= reference:
            candidate += timedelta(days=7)
        return candidate

    if schedule_type == "monthly":
        target_dom = int(rule.get("day_of_month", 1))
        candidate = ref_local.replace(
            hour=fire_time.hour, minute=fire_time.minute, second=0, microsecond=0
        )

        def _set_day(dt: datetime, day: int) -> datetime:
            # Clamp to last day of month if target_dom > month length.
            year, month = dt.year, dt.month
            # Days in month: simplest is to advance to next month's day 1 and back off 1 day.
            if month == 12:
                last = (datetime(year + 1, 1, 1) - timedelta(days=1)).day
            else:
                last = (datetime(year, month + 1, 1) - timedelta(days=1)).day
            return dt.replace(day=min(day, last))

        candidate = _set_day(candidate, target_dom)
        if candidate <= reference:
            # Roll into the next month.
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1, day=1)
            else:
                candidate = candidate.replace(month=candidate.month + 1, day=1)
            candidate = _set_day(candidate, target_dom)
        return candidate

    return None


def _format_rule(rule: dict[str, Any]) -> str:
    st = rule.get("schedule_type", "?")
    t = rule.get("time_of_day", "?")
    if st == "daily":
        return f"daily at {t}"
    if st == "weekdays":
        return f"weekdays at {t}"
    if st == "weekly":
        wd = rule.get("weekday", 0)
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        try:
            wd_name = names[int(wd)]
        except (TypeError, ValueError, IndexError):
            wd_name = "?"
        return f"weekly on {wd_name} at {t}"
    if st == "monthly":
        return f"monthly on day {rule.get('day_of_month', '?')} at {t}"
    return f"{st} at {t}"


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
        "remind_recurring",
        (
            "Schedule a repeating reminder. Use when the principal asks to be "
            "pinged on a regular cadence (daily, weekdays, weekly on a "
            "specific day, or monthly on a specific date).\n\n"
            "schedule_type:\n"
            "  - 'daily'    — every day at time_of_day\n"
            "  - 'weekdays' — Monday through Friday at time_of_day\n"
            "  - 'weekly'   — every week on `weekday` at time_of_day\n"
            "                 weekday is 0 (Mon) … 6 (Sun) or a name like 'tuesday'\n"
            "  - 'monthly'  — every month on `day_of_month` at time_of_day\n"
            "                 if a month has fewer days, fires on the last day\n\n"
            "time_of_day is HH:MM 24-hour in the principal's timezone.\n\n"
            "The `message` is sent verbatim each time the reminder fires — "
            "write it as the thing the principal will read, in your voice."
        ),
        {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "time_of_day": {
                    "type": "string",
                    "description": "HH:MM 24-hour, principal's local timezone.",
                },
                "schedule_type": {
                    "type": "string",
                    "enum": list(SCHEDULE_TYPES),
                },
                "weekday": {
                    "description": "Required when schedule_type='weekly'. 0=Mon..6=Sun, or a name like 'tuesday'.",
                },
                "day_of_month": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 31,
                    "description": "Required when schedule_type='monthly'.",
                },
            },
            "required": ["message", "time_of_day", "schedule_type"],
        },
    )
    async def remind_recurring(args: dict[str, Any]) -> dict[str, Any]:
        schedule_type = args["schedule_type"]
        if schedule_type not in SCHEDULE_TYPES:
            return _err(f"unknown schedule_type {schedule_type!r}")

        if _parse_hhmm(args["time_of_day"]) is None:
            return _err(
                f"time_of_day must be HH:MM (24-hour), got {args['time_of_day']!r}"
            )

        rule: dict[str, Any] = {
            "schedule_type": schedule_type,
            "time_of_day": args["time_of_day"],
            "tz": _user_tz_name(),
        }

        if schedule_type == "weekly":
            wd_raw = args.get("weekday")
            if wd_raw is None:
                return _err("weekly schedule requires `weekday`")
            if isinstance(wd_raw, str):
                wd = WEEKDAY_LOOKUP.get(wd_raw.strip().lower())
                if wd is None:
                    return _err(f"unknown weekday {wd_raw!r}")
            else:
                try:
                    wd = int(wd_raw)
                except (TypeError, ValueError):
                    return _err(f"weekday must be 0..6 or a day name; got {wd_raw!r}")
                if not (0 <= wd <= 6):
                    return _err(f"weekday must be 0..6; got {wd}")
            rule["weekday"] = wd

        if schedule_type == "monthly":
            dom = args.get("day_of_month")
            if dom is None:
                return _err("monthly schedule requires `day_of_month`")
            try:
                dom_int = int(dom)
            except (TypeError, ValueError):
                return _err(f"day_of_month must be an int 1..31; got {dom!r}")
            if not (1 <= dom_int <= 31):
                return _err(f"day_of_month must be 1..31; got {dom_int}")
            rule["day_of_month"] = dom_int

        # Compute the FIRST fire time. Pass datetime.now(UTC) as reference.
        from datetime import timezone as _tz
        first_fire = _next_recurrence(rule, datetime.now(_tz.utc))
        if first_fire is None:
            return _err("failed to compute first fire time — bad rule")

        rid = store.schedule_reminder(
            fire_at=first_fire.isoformat(),
            message=args["message"],
            recurrence_rule=rule,
        )
        return _ok(
            f"recurring reminder #{rid} scheduled "
            f"({_format_rule(rule)}). first fire: "
            f"{first_fire.strftime('%Y-%m-%d %H:%M %Z')}.\n"
            f"message: {args['message']}"
        )

    @tool(
        "list_reminders",
        "List all pending reminders (one-off + recurring, not yet fired/cancelled).",
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
            rule_raw = r.get("recurrence_rule")
            if rule_raw:
                import json as _json
                try:
                    rule = _json.loads(rule_raw)
                    cadence = f" [{_format_rule(rule)}]"
                except (TypeError, ValueError):
                    cadence = " [recurring: ?]"
            else:
                cadence = ""
            lines.append(f"- #{r['id']} @ {when}{cadence}: {r['message']}")
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
        tools=[remind, remind_recurring, list_reminders, cancel_reminder],
    )


def main() -> None:
    raise NotImplementedError(
        "reminders_server is in-process; instantiate via create_reminders_mcp_server(store) from agent_host."
    )


if __name__ == "__main__":
    main()
