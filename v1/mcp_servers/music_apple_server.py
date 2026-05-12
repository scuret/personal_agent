"""Apple Music.app MCP server (macOS-only, via AppleScript).

Local playback control for Music.app on the Mac. Coexists with Spotify
— different tool namespace (music_apple_*) so the agent can pick based
on which service the principal mentions. AppleScript only reaches the
Music app on this Mac; phone playback isn't controllable.

Tools (namespaced as mcp__music_apple__<name>):
  music_apple_now_playing
  music_apple_play
  music_apple_pause
  music_apple_next
  music_apple_previous
  music_apple_search_and_play(query)
  music_apple_list_playlists
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.applescript import err, escape_str, ok, run_script


def create_music_apple_mcp_server() -> McpSdkServerConfig:
    @tool(
        "music_apple_now_playing",
        "What's currently playing in Music.app on this Mac. Reports paused/stopped if applicable.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def now_playing(_args: dict[str, Any]) -> dict[str, Any]:
        script = '''
        tell application "Music"
            set st to player state as string
            if st is "stopped" then
                return "stopped"
            end if
            try
                set tname to name of current track
                set tartist to artist of current track
                set talbum to album of current track
                return st & " | " & tname & " — " & tartist & " (" & talbum & ")"
            on error
                return st & " | (no track loaded)"
            end try
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(raw or "(no response from Music)")

    @tool(
        "music_apple_play",
        "Resume Music.app playback (or start the current track if stopped).",
        {"type": "object", "properties": {}, "required": []},
    )
    async def play(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            run_script('tell application "Music" to play')
        except RuntimeError as e:
            return err(str(e))
        return ok("playing.")

    @tool(
        "music_apple_pause",
        "Pause Music.app playback.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def pause(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            run_script('tell application "Music" to pause')
        except RuntimeError as e:
            return err(str(e))
        return ok("paused.")

    @tool(
        "music_apple_next",
        "Skip to the next track.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def next_track(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            run_script('tell application "Music" to next track')
        except RuntimeError as e:
            return err(str(e))
        return ok("skipped.")

    @tool(
        "music_apple_previous",
        "Go to the previous track (or restart current).",
        {"type": "object", "properties": {}, "required": []},
    )
    async def previous_track(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            run_script('tell application "Music" to previous track')
        except RuntimeError as e:
            return err(str(e))
        return ok("previous.")

    @tool(
        "music_apple_search_and_play",
        (
            "Search the local Music library for `query` (matched against "
            "track names) and play the first hit. Library matches only — "
            "doesn't reach Apple Music's catalog."
        ),
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    async def search_and_play(args: dict[str, Any]) -> dict[str, Any]:
        q = escape_str(args["query"])
        script = f'''
        tell application "Music"
            set hits to (tracks of library playlist 1 whose name contains "{q}")
            if (count of hits) is 0 then
                return "no matches"
            end if
            set firstHit to item 1 of hits
            play firstHit
            return (name of firstHit) & " — " & (artist of firstHit)
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        if raw == "no matches":
            return err("no library tracks matched.")
        return ok(f"playing: {raw}")

    @tool(
        "music_apple_list_playlists",
        "List Music.app playlists in the local library.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_playlists(_args: dict[str, Any]) -> dict[str, Any]:
        script = '''
        tell application "Music"
            set out to ""
            repeat with p in user playlists
                set out to out & (name of p as string) & linefeed
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
            return ok("(no playlists)")
        return ok("\n".join(f"- {n}" for n in names))

    return create_sdk_mcp_server(
        name="music_apple",
        version="1.0.0",
        tools=[
            now_playing, play, pause, next_track, previous_track,
            search_and_play, list_playlists,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "music_apple_server is in-process; instantiate via create_music_apple_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
