"""Apple Photos.app MCP server (macOS-only, via AppleScript).

Read-only access to the local Photos library — albums, recents,
date-range listings, album contents. AppleScript can't reach Photos'
ML face/object/place index (that lives in Photos' private SQLite +
PHAsset Cocoa APIs), so semantic content search ("photos of grayson"
or "photos at the beach") is out of scope for v1.

Tools (namespaced as mcp__photos_apple__<name>):
  photos_apple_list_albums
  photos_apple_recent(limit?)         — N most recent items
  photos_apple_search_by_date(start, end, limit?)
  photos_apple_get_album(name, limit?)

Each photo result includes its filename, date, and Photos-internal id.
For actual pixels, use Photos.app on the Mac — surfacing image binaries
via AppleScript is slow + fragile.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.applescript import err, escape_str, ok, run_script


def _format_photo_rows(raw: str) -> str:
    rows = [line for line in raw.splitlines() if line.strip()]
    if not rows:
        return "(no photos)"
    out: list[str] = []
    for row in rows:
        parts = row.split("|", 2)
        pid = parts[0] if parts else "?"
        date = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else "(unnamed)"
        line = f"- [{pid}] {name}"
        if date:
            line += f"  ({date})"
        out.append(line)
    return "\n".join(out)


def create_photos_apple_mcp_server() -> McpSdkServerConfig:
    @tool(
        "photos_apple_list_albums",
        "List all albums in the Photos library by name.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_albums(_args: dict[str, Any]) -> dict[str, Any]:
        script = '''
        tell application "Photos"
            set out to ""
            repeat with a in albums
                set out to out & (name of a as string) & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        names = [line for line in raw.splitlines() if line.strip()]
        if not names:
            return ok("(no albums)")
        return ok("\n".join(f"- {n}" for n in names))

    @tool(
        "photos_apple_recent",
        "Most recently added photos.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
        },
    )
    async def recent_photos(args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 20))
        script = f'''
        tell application "Photos"
            set out to ""
            set items_ to media items
            set total to count of items_
            set startN to total - {limit} + 1
            if startN < 1 then set startN to 1
            repeat with i from total to startN by -1
                set m to item i of items_
                set mid_ to id of m
                set mdate to (date of m as string)
                set mname to ""
                try
                    set mname to (filename of m as string)
                end try
                set out to out & mid_ & "|" & mdate & "|" & mname & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(_format_photo_rows(raw))

    @tool(
        "photos_apple_search_by_date",
        (
            "Photos taken within a date range. Use ISO date strings "
            "YYYY-MM-DD (start is inclusive, end is exclusive)."
        ),
        {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "YYYY-MM-DD inclusive"},
                "end":   {"type": "string", "description": "YYYY-MM-DD exclusive"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["start", "end"],
        },
    )
    async def search_by_date(args: dict[str, Any]) -> dict[str, Any]:
        start = escape_str(args["start"])
        end = escape_str(args["end"])
        limit = int(args.get("limit", 50))
        # AppleScript dates are parsed in the local timezone. Construct
        # them via `current date` + manipulation rather than string parse
        # for reliability.
        script = f'''
        tell application "Photos"
            set startDate to (date "{start}")
            set endDate to (date "{end}")
            set out to ""
            set items_ to (media items whose date ≥ startDate and date < endDate)
            set n to 0
            repeat with m in items_
                if n ≥ {limit} then exit repeat
                set n to n + 1
                set mid_ to id of m
                set mdate to (date of m as string)
                set mname to ""
                try
                    set mname to (filename of m as string)
                end try
                set out to out & mid_ & "|" & mdate & "|" & mname & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(_format_photo_rows(raw))

    @tool(
        "photos_apple_get_album",
        "List photos in an album by name. Match is exact-string.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["name"],
        },
    )
    async def get_album(args: dict[str, Any]) -> dict[str, Any]:
        name = escape_str(args["name"])
        limit = int(args.get("limit", 50))
        script = f'''
        tell application "Photos"
            set out to ""
            set targetAlbum to album "{name}"
            set items_ to media items of targetAlbum
            set n to 0
            repeat with m in items_
                if n ≥ {limit} then exit repeat
                set n to n + 1
                set mid_ to id of m
                set mdate to (date of m as string)
                set mname to ""
                try
                    set mname to (filename of m as string)
                end try
                set out to out & mid_ & "|" & mdate & "|" & mname & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(_format_photo_rows(raw))

    return create_sdk_mcp_server(
        name="photos_apple",
        version="1.0.0",
        tools=[list_albums, recent_photos, search_by_date, get_album],
    )


def main() -> None:
    raise NotImplementedError(
        "photos_apple_server is in-process; instantiate via create_photos_apple_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
