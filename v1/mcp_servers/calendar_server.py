"""Google Calendar MCP server — READ-ONLY in v1.

Uses the Calendar API v3 via the shared google_auth helper. The OAuth
scope is calendar.readonly, so the agent literally cannot create, update,
or delete events through this app — even if a future contributor exposed
a write tool, the API would reject it.

Tools exposed (namespaced as mcp__calendar__<name>):

  calendar_list_events(time_min?, time_max?, calendar_id?, max_results?)
      List events in a time range. Defaults: time_min=now, time_max=now+7d.

  calendar_search_events(query, time_min?, time_max?, calendar_id?)
      Substring search over event titles/descriptions in a time range.

  calendar_check_availability(time_min, time_max, calendar_ids?)
      Free/busy check across one or more calendars. Returns busy intervals.

  calendar_get_event(event_id, calendar_id?)
      Full event details for one ID.

INTENTIONALLY MISSING in v1: create / update / delete event tools.
Calendar writes are deferred to v2 once read patterns are stable.

Time formats: RFC3339 / ISO 8601 strings. The agent gets the current
local date/time/timezone in its system prompt, so it can format these
correctly without us doing fancy parsing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from googleapiclient.errors import HttpError

from mcp_servers.google_auth import build_service


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _calendar():
    return build_service("calendar", "v3")


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_window(days: int = 7) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (
        now.isoformat().replace("+00:00", "Z"),
        (now + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
    )


def _format_event(e: dict[str, Any]) -> str:
    summary = e.get("summary", "(no title)")
    # All-day events use `date`; timed use `dateTime`.
    start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "?")
    end = e.get("end", {}).get("dateTime") or e.get("end", {}).get("date", "?")
    location = e.get("location", "")
    line = f"- [{e.get('id', '?')}] {start} → {end} | {summary}"
    if location:
        line += f" @ {location}"
    return line


def create_calendar_mcp_server() -> McpSdkServerConfig:
    @tool(
        "calendar_list_events",
        (
            "List Google Calendar events in a time window. Defaults: "
            "time_min=now, time_max=now+7d. Times are RFC3339/ISO 8601 "
            "strings (e.g. '2026-05-08T14:00:00-05:00'). Returns id, "
            "start, end, summary, location for each event."
        ),
        {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "ISO 8601 start. Default: now."},
                "time_max": {"type": "string", "description": "ISO 8601 end. Default: now+7d."},
                "calendar_id": {
                    "type": "string",
                    "description": "Calendar ID. Default 'primary'.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Max events to return. Default 25.",
                },
            },
            "required": [],
        },
    )
    async def calendar_list_events(args: dict[str, Any]) -> dict[str, Any]:
        default_min, default_max = _default_window()
        try:
            resp = (
                _calendar()
                .events()
                .list(
                    calendarId=args.get("calendar_id", "primary"),
                    timeMin=args.get("time_min", default_min),
                    timeMax=args.get("time_max", default_max),
                    maxResults=int(args.get("max_results", 25)),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except HttpError as e:
            return _err(f"calendar list_events failed: {e}")
        events = resp.get("items", [])
        if not events:
            return _ok("no events in that window.")
        return _ok("\n".join(_format_event(e) for e in events))

    @tool(
        "calendar_search_events",
        (
            "Substring search Calendar events by keyword in title/description. "
            "Optional time window narrows the search."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
                "calendar_id": {"type": "string"},
            },
            "required": ["query"],
        },
    )
    async def calendar_search_events(args: dict[str, Any]) -> dict[str, Any]:
        try:
            params: dict[str, Any] = {
                "calendarId": args.get("calendar_id", "primary"),
                "q": args["query"],
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 25,
            }
            if args.get("time_min"):
                params["timeMin"] = args["time_min"]
            if args.get("time_max"):
                params["timeMax"] = args["time_max"]
            resp = _calendar().events().list(**params).execute()
        except HttpError as e:
            return _err(f"calendar search_events failed: {e}")
        events = resp.get("items", [])
        if not events:
            return _ok("no matching events.")
        return _ok("\n".join(_format_event(e) for e in events))

    @tool(
        "calendar_check_availability",
        (
            "Free/busy check across one or more calendars. Returns busy "
            "intervals only (so empty list = fully free)."
        ),
        {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "ISO 8601 start."},
                "time_max": {"type": "string", "description": "ISO 8601 end."},
                "calendar_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of calendar IDs to check. Default ['primary'].",
                },
            },
            "required": ["time_min", "time_max"],
        },
    )
    async def calendar_check_availability(args: dict[str, Any]) -> dict[str, Any]:
        cal_ids = args.get("calendar_ids") or ["primary"]
        try:
            resp = (
                _calendar()
                .freebusy()
                .query(
                    body={
                        "timeMin": args["time_min"],
                        "timeMax": args["time_max"],
                        "items": [{"id": cid} for cid in cal_ids],
                    }
                )
                .execute()
            )
        except HttpError as e:
            return _err(f"calendar check_availability failed: {e}")
        cals = resp.get("calendars", {})
        if not cals:
            return _ok("no calendar data returned.")
        lines = []
        for cid, info in cals.items():
            busy = info.get("busy", [])
            if not busy:
                lines.append(f"{cid}: free")
            else:
                for b in busy:
                    lines.append(f"{cid}: busy {b['start']} → {b['end']}")
        return _ok("\n".join(lines))

    @tool(
        "calendar_get_event",
        "Get full details of a single Calendar event by ID.",
        {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "description": "Default 'primary'."},
            },
            "required": ["event_id"],
        },
    )
    async def calendar_get_event(args: dict[str, Any]) -> dict[str, Any]:
        try:
            e = (
                _calendar()
                .events()
                .get(
                    calendarId=args.get("calendar_id", "primary"),
                    eventId=args["event_id"],
                )
                .execute()
            )
        except HttpError as err:
            return _err(f"calendar get_event failed: {err}")
        attendees = e.get("attendees", [])
        attendee_str = (
            ", ".join(a.get("email", "?") for a in attendees) if attendees else "(none)"
        )
        text = (
            f"id: {e.get('id', '')}\n"
            f"summary: {e.get('summary', '(no title)')}\n"
            f"start: {e.get('start', {}).get('dateTime') or e.get('start', {}).get('date', '?')}\n"
            f"end:   {e.get('end', {}).get('dateTime') or e.get('end', {}).get('date', '?')}\n"
            f"location: {e.get('location', '')}\n"
            f"organizer: {e.get('organizer', {}).get('email', '')}\n"
            f"attendees: {attendee_str}\n"
            f"status: {e.get('status', '')}\n\n"
            f"{e.get('description', '')[:2000]}"
        )
        return _ok(text)

    return create_sdk_mcp_server(
        name="calendar",
        version="1.0.0",
        tools=[
            calendar_list_events,
            calendar_search_events,
            calendar_check_availability,
            calendar_get_event,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "calendar_server is in-process; instantiate via create_calendar_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
