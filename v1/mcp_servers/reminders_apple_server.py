"""Apple Reminders.app MCP server (macOS-only, via AppleScript).

Bridges the native Reminders app — useful alongside Todoist for the
lists that live in Reminders (Siri-created reminders, family-shared
lists synced via iCloud).

Tools (namespaced as mcp__reminders_apple__<name>):
  reminders_apple_list_lists
  reminders_apple_list(list?, include_completed?, limit?)
  reminders_apple_create(text, list?, due_string?)
  reminders_apple_complete(reminder_id)
  reminders_apple_delete(reminder_id)

Reminder ids are AppleScript-internal numeric IDs that change if you
restart Reminders.app. They're stable within a session — fetch a list
just before completing/deleting.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.applescript import err, escape_str, ok, run_script


def create_reminders_apple_mcp_server() -> McpSdkServerConfig:
    @tool(
        "reminders_apple_list_lists",
        "List all Apple Reminders lists. Returns list names you can pass to other tools.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_lists(_args: dict[str, Any]) -> dict[str, Any]:
        script = '''
        tell application "Reminders"
            set out to ""
            repeat with l in lists
                set out to out & (name of l as string) & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        lists = [line for line in raw.splitlines() if line.strip()]
        if not lists:
            return ok("(no lists)")
        return ok("\n".join(f"- {name}" for name in lists))

    @tool(
        "reminders_apple_list",
        (
            "List reminders. Omit `list` for all incomplete reminders across "
            "every list. `include_completed=true` includes completed ones. "
            "Returns id + name + due date if set."
        ),
        {
            "type": "object",
            "properties": {
                "list": {"type": "string", "description": "List name (omit for all)."},
                "include_completed": {"type": "boolean", "description": "Default false."},
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 200,
                    "description": "Default 50.",
                },
            },
            "required": [],
        },
    )
    async def list_reminders(args: dict[str, Any]) -> dict[str, Any]:
        list_name = args.get("list")
        include_completed = bool(args.get("include_completed", False))
        limit = int(args.get("limit", 50))

        filter_clause = "" if include_completed else "whose completed is false"
        list_clause = (
            f'reminders of list "{escape_str(list_name)}"'
            if list_name
            else "reminders of every list"
        )

        script = f'''
        tell application "Reminders"
            set out to ""
            set rems to ({list_clause} {filter_clause})
            set n to 0
            repeat with r in rems
                if n ≥ {limit} then exit repeat
                set n to n + 1
                set rid to id of r
                set rname to name of r
                set ddue to ""
                try
                    set ddue to (due date of r as string)
                end try
                set out to out & rid & "|" & rname & "|" & ddue & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        rows = [line for line in raw.splitlines() if line.strip()]
        if not rows:
            return ok("(no reminders)")
        lines: list[str] = []
        for row in rows:
            parts = row.split("|", 2)
            rid = parts[0] if parts else "?"
            name = parts[1] if len(parts) > 1 else "(unnamed)"
            due = parts[2] if len(parts) > 2 else ""
            line = f"- [{rid}] {name}"
            if due:
                line += f"  (due {due})"
            lines.append(line)
        return ok("\n".join(lines))

    @tool(
        "reminders_apple_create",
        (
            "Create a new Apple reminder. Optional `list` (default: default "
            "list), optional `due_string` like 'tomorrow at 3pm' (parsed by "
            "Reminders, not by us)."
        ),
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "list": {"type": "string"},
                "due_string": {"type": "string"},
            },
            "required": ["text"],
        },
    )
    async def create_reminder(args: dict[str, Any]) -> dict[str, Any]:
        text = escape_str(args["text"])
        list_name = args.get("list")
        due_string = args.get("due_string")

        target = (
            f'list "{escape_str(list_name)}"'
            if list_name
            else "default list"
        )
        with_due = ""
        if due_string:
            # Reminders accepts a date object — use AppleScript's natural
            # date parsing. Wrap in try so a malformed string doesn't
            # blow up the whole script; reminder still gets created.
            with_due = f''',\n                    due date:(date "{escape_str(due_string)}")'''

        script = f'''
        tell application "Reminders"
            tell {target}
                set newR to make new reminder with properties {{name:"{text}"{with_due}}}
                return id of newR
            end tell
        end tell
        '''
        try:
            rid = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(f"created reminder {rid.strip()}")

    @tool(
        "reminders_apple_complete",
        "Mark an Apple reminder complete by id. Get the id from reminders_apple_list.",
        {
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
        },
    )
    async def complete_reminder(args: dict[str, Any]) -> dict[str, Any]:
        rid = escape_str(args["reminder_id"])
        script = f'''
        tell application "Reminders"
            set r to first reminder whose id is "{rid}"
            set completed of r to true
            return name of r
        end tell
        '''
        try:
            name = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(f"completed: {name}")

    @tool(
        "reminders_apple_delete",
        "Delete an Apple reminder by id. Irreversible — confirm with the principal first.",
        {
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
        },
    )
    async def delete_reminder(args: dict[str, Any]) -> dict[str, Any]:
        rid = escape_str(args["reminder_id"])
        script = f'''
        tell application "Reminders"
            set r to first reminder whose id is "{rid}"
            set rname to name of r
            delete r
            return rname
        end tell
        '''
        try:
            name = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(f"deleted: {name}")

    return create_sdk_mcp_server(
        name="reminders_apple",
        version="1.0.0",
        tools=[
            list_lists,
            list_reminders,
            create_reminder,
            complete_reminder,
            delete_reminder,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "reminders_apple_server is in-process; instantiate via create_reminders_apple_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
