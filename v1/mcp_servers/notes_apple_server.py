"""Apple Notes.app MCP server (macOS-only, via AppleScript).

Tools (namespaced as mcp__notes_apple__<name>):
  notes_apple_list(folder?, limit?)
  notes_apple_search(query, limit?)
  notes_apple_read(title)              — first match by title
  notes_apple_append(title, text)      — appends to body, preserves
                                         existing content
  notes_apple_create(title, body?, folder?)

Notes' AppleScript surface is read-write but doesn't expose rich text
nicely — we operate on plaintext body. Notes uses HTML internally so
appended text appears as a new paragraph.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.applescript import err, escape_str, ok, run_script


def create_notes_apple_mcp_server() -> McpSdkServerConfig:
    @tool(
        "notes_apple_list",
        (
            "List notes by title. Optional `folder` filter. Returns titles "
            "only (use notes_apple_read to fetch a body)."
        ),
        {
            "type": "object",
            "properties": {
                "folder": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": [],
        },
    )
    async def list_notes(args: dict[str, Any]) -> dict[str, Any]:
        folder = args.get("folder")
        limit = int(args.get("limit", 50))
        source = (
            f'notes of folder "{escape_str(folder)}"'
            if folder
            else "every note"
        )
        script = f'''
        tell application "Notes"
            set out to ""
            set ns to {source}
            set n to 0
            repeat with note_ in ns
                if n ≥ {limit} then exit repeat
                set n to n + 1
                set out to out & (name of note_ as string) & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        titles = [line for line in raw.splitlines() if line.strip()]
        if not titles:
            return ok("(no notes)")
        return ok("\n".join(f"- {t}" for t in titles))

    @tool(
        "notes_apple_search",
        (
            "Substring search across note titles + bodies. Returns matching "
            "titles. Use notes_apple_read with one of them to get the body."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    )
    async def search_notes(args: dict[str, Any]) -> dict[str, Any]:
        q = escape_str(args["query"])
        limit = int(args.get("limit", 20))
        script = f'''
        tell application "Notes"
            set out to ""
            set hits to notes whose name contains "{q}" or body contains "{q}"
            set n to 0
            repeat with note_ in hits
                if n ≥ {limit} then exit repeat
                set n to n + 1
                set out to out & (name of note_ as string) & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        titles = [line for line in raw.splitlines() if line.strip()]
        if not titles:
            return ok("(no matches)")
        return ok("\n".join(f"- {t}" for t in titles))

    @tool(
        "notes_apple_read",
        "Read the body of the first note matching `title` (substring match).",
        {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    )
    async def read_note(args: dict[str, Any]) -> dict[str, Any]:
        title = escape_str(args["title"])
        script = f'''
        tell application "Notes"
            set target to first note whose name contains "{title}"
            set out to (name of target) & linefeed & "---" & linefeed & (plaintext of target)
            return out
        end tell
        '''
        try:
            body = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(body or "(empty note)")

    @tool(
        "notes_apple_append",
        (
            "Append `text` to the first note whose title contains the given "
            "title string. Text lands as a new paragraph at the end of "
            "the existing body."
        ),
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["title", "text"],
        },
    )
    async def append_note(args: dict[str, Any]) -> dict[str, Any]:
        title = escape_str(args["title"])
        text = escape_str(args["text"])
        # Notes' body is HTML — newlines need to be <br> for paragraphing.
        # Wrap the appended text in a <div> so it forms its own block.
        script = f'''
        tell application "Notes"
            set target to first note whose name contains "{title}"
            set body of target to (body of target) & "<div>{text}</div>"
            return name of target
        end tell
        '''
        try:
            name = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(f"appended to: {name}")

    @tool(
        "notes_apple_create",
        "Create a new note. Optional `folder` (default: top-level account folder).",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "folder": {"type": "string"},
            },
            "required": ["title"],
        },
    )
    async def create_note(args: dict[str, Any]) -> dict[str, Any]:
        title = escape_str(args["title"])
        body = escape_str(args.get("body") or "")
        folder = args.get("folder")
        # Notes wants HTML for body, with <h1> as the first line title-style.
        # AppleScript's `make new note` auto-detects the title from the
        # body's first line if not set explicitly.
        html_body = f"<h1>{title}</h1><div>{body}</div>" if body else f"<h1>{title}</h1>"
        target = (
            f'folder "{escape_str(folder)}"'
            if folder
            else "default folder of default account"
        )
        script = f'''
        tell application "Notes"
            tell {target}
                set newNote to make new note with properties {{body:"{html_body}"}}
                return name of newNote
            end tell
        end tell
        '''
        try:
            name = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(f"created: {name}")

    return create_sdk_mcp_server(
        name="notes_apple",
        version="1.0.0",
        tools=[list_notes, search_notes, read_note, append_note, create_note],
    )


def main() -> None:
    raise NotImplementedError(
        "notes_apple_server is in-process; instantiate via create_notes_apple_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
