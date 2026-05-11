"""Spotify MCP server — search, playback, queue, library.

Uses the Spotify Web API. Auth flows through `mcp_servers.spotify_auth`
(OAuth refresh-token flow). See that module for first-time setup.

Tools exposed (namespaced as mcp__spotify__<name>):

  spotify_search(query, type?, limit?)
      Search tracks / artists / albums / playlists. Returns name + URI
      for each result — agent passes the URI back to play/queue tools.

  spotify_currently_playing()
      What's playing now. Returns track name, artists, album, progress,
      device name. Handles "nothing playing" gracefully.

  spotify_play(uri?, device_id?)
      Start or resume playback. If `uri` is omitted, resumes whatever
      was paused. Track URIs play the track; playlist/album/artist URIs
      start a context (queue of tracks from that source).

  spotify_pause()
      Pause playback on the active device.

  spotify_queue(uri, device_id?)
      Add a track to the playback queue (after the current track).

  spotify_list_playlists(limit?)
      List the principal's own playlists. Returns name, id, track count.

  spotify_list_devices()
      List available playback devices (phone, Mac, web player, etc.)
      with id + name + type. Use these IDs with play/queue.

Playback requires an active Spotify Premium device. If nothing is
playing or no device is active, the API returns 404 NO_ACTIVE_DEVICE —
tell the principal to open Spotify somewhere first.
"""

from __future__ import annotations

from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.spotify_auth import get_access_token

API_BASE = "https://api.spotify.com/v1"
TIMEOUT_S = 15


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_access_token()}"}


def _request(method: str, path: str, **kwargs: Any) -> requests.Response:
    """Wrapper that injects the bearer header and base URL."""
    headers = kwargs.pop("headers", {}) or {}
    headers.update(_headers())
    return requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=TIMEOUT_S, **kwargs)


def _explain_error(resp: requests.Response) -> str:
    """Translate Spotify HTTP errors into agent-readable messages."""
    if resp.status_code == 401:
        return "spotify auth expired or invalid — token cache may be corrupted."
    if resp.status_code == 403:
        return (
            "spotify forbidden. most playback endpoints require Premium; "
            "if this is a free account, playback control isn't available."
        )
    if resp.status_code == 404:
        # The /me/player endpoints return 404 with reason=NO_ACTIVE_DEVICE
        # if no Spotify client is currently active.
        try:
            body = resp.json()
            reason = (body.get("error") or {}).get("reason", "")
            if reason == "NO_ACTIVE_DEVICE":
                return (
                    "no active Spotify device. open Spotify on a phone, "
                    "Mac, or web player first, then retry."
                )
        except ValueError:
            pass
        return f"spotify resource not found (HTTP 404): {resp.text[:200]}"
    if resp.status_code == 429:
        return "spotify rate-limited. wait a few seconds and retry."
    return f"spotify HTTP {resp.status_code}: {resp.text[:300]}"


def _format_track(t: dict[str, Any]) -> str:
    name = t.get("name", "(unknown)")
    artists = ", ".join(a.get("name", "?") for a in (t.get("artists") or []))
    album = (t.get("album") or {}).get("name", "")
    uri = t.get("uri", "")
    line = f"- {name} — {artists}"
    if album:
        line += f" ({album})"
    if uri:
        line += f" [{uri}]"
    return line


def create_spotify_mcp_server() -> McpSdkServerConfig:
    @tool(
        "spotify_search",
        (
            "Search Spotify. `type` is one of 'track', 'artist', 'album', "
            "'playlist' (default 'track'). Returns up to `limit` results "
            "(default 10, max 50). Each result includes a URI you can "
            "pass to spotify_play or spotify_queue."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": ["track", "artist", "album", "playlist"],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        },
    )
    async def spotify_search(args: dict[str, Any]) -> dict[str, Any]:
        search_type = args.get("type", "track")
        params = {
            "q": args["query"],
            "type": search_type,
            "limit": int(args.get("limit", 10)),
        }
        try:
            resp = _request("GET", "/search", params=params)
        except requests.RequestException as e:
            return _err(f"spotify search failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        data = resp.json()
        # The response key is pluralized: 'tracks', 'artists', etc.
        items = (data.get(f"{search_type}s") or {}).get("items") or []
        if not items:
            return _ok("(no matches)")
        if search_type == "track":
            return _ok("\n".join(_format_track(t) for t in items))
        # For non-tracks, render name + uri.
        return _ok(
            "\n".join(
                f"- {it.get('name', '?')} [{it.get('uri', '')}]" for it in items
            )
        )

    @tool(
        "spotify_currently_playing",
        (
            "Return what's playing right now: track, artists, album, "
            "progress, and the device. Returns 'nothing playing' if "
            "the player is idle."
        ),
        {"type": "object", "properties": {}, "required": []},
    )
    async def spotify_currently_playing(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = _request("GET", "/me/player/currently-playing")
        except requests.RequestException as e:
            return _err(f"spotify currently_playing failed: {e}")
        if resp.status_code == 204:
            return _ok("nothing playing.")
        if not resp.ok:
            return _err(_explain_error(resp))
        data = resp.json()
        item = data.get("item") or {}
        name = item.get("name", "(unknown)")
        artists = ", ".join(a.get("name", "?") for a in (item.get("artists") or []))
        album = (item.get("album") or {}).get("name", "")
        progress_ms = int(data.get("progress_ms") or 0)
        duration_ms = int(item.get("duration_ms") or 0)
        is_playing = bool(data.get("is_playing"))
        device = (data.get("device") or {}).get("name", "?")
        state = "▶ playing" if is_playing else "⏸ paused"
        return _ok(
            f"{state}: {name} — {artists}\n"
            f"album: {album}\n"
            f"progress: {progress_ms // 1000}s / {duration_ms // 1000}s\n"
            f"device: {device}"
        )

    @tool(
        "spotify_play",
        (
            "Start or resume Spotify playback. If `uri` is a track URI "
            "(spotify:track:...), play that track. If it's a playlist "
            "or album URI, play that context. Omit `uri` to resume "
            "whatever was paused. Requires an active Premium device."
        ),
        {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "Spotify URI (track / playlist / album / artist). Omit to resume.",
                },
                "device_id": {
                    "type": "string",
                    "description": "Optional device ID from spotify_list_devices. Defaults to whatever's active.",
                },
            },
            "required": [],
        },
    )
    async def spotify_play(args: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        uri = args.get("uri")
        if uri:
            if uri.startswith("spotify:track:"):
                body["uris"] = [uri]
            else:
                body["context_uri"] = uri
        params: dict[str, Any] = {}
        if args.get("device_id"):
            params["device_id"] = args["device_id"]
        try:
            resp = _request("PUT", "/me/player/play", params=params, json=body or None)
        except requests.RequestException as e:
            return _err(f"spotify play failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        return _ok("playback started." if uri else "resumed.")

    @tool(
        "spotify_pause",
        "Pause playback on the active Spotify device.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def spotify_pause(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = _request("PUT", "/me/player/pause")
        except requests.RequestException as e:
            return _err(f"spotify pause failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        return _ok("paused.")

    @tool(
        "spotify_queue",
        (
            "Add a track to the Spotify playback queue. The URI must be "
            "a track URI (spotify:track:...). Plays after the current "
            "track finishes — does not interrupt."
        ),
        {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "Track URI (spotify:track:...)."},
                "device_id": {"type": "string"},
            },
            "required": ["uri"],
        },
    )
    async def spotify_queue(args: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {"uri": args["uri"]}
        if args.get("device_id"):
            params["device_id"] = args["device_id"]
        try:
            resp = _request("POST", "/me/player/queue", params=params)
        except requests.RequestException as e:
            return _err(f"spotify queue failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        return _ok(f"queued {args['uri']}.")

    @tool(
        "spotify_list_playlists",
        (
            "List the principal's playlists (those they own AND those "
            "they follow). Returns name, id, track count, and URI for "
            "each. Use the URI with spotify_play to play the playlist."
        ),
        {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Default 20.",
                },
            },
            "required": [],
        },
    )
    async def spotify_list_playlists(args: dict[str, Any]) -> dict[str, Any]:
        params = {"limit": int(args.get("limit", 20))}
        try:
            resp = _request("GET", "/me/playlists", params=params)
        except requests.RequestException as e:
            return _err(f"spotify list_playlists failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        items = (resp.json() or {}).get("items") or []
        if not items:
            return _ok("(no playlists)")
        lines = []
        for p in items:
            tracks = (p.get("tracks") or {}).get("total", "?")
            lines.append(
                f"- {p.get('name', '?')} ({tracks} tracks) [{p.get('uri', '')}]"
            )
        return _ok("\n".join(lines))

    @tool(
        "spotify_list_devices",
        (
            "List available Spotify playback devices. Returns id, name, "
            "type (Smartphone, Computer, etc.), and whether each is "
            "currently active. Use the id with play/queue's device_id."
        ),
        {"type": "object", "properties": {}, "required": []},
    )
    async def spotify_list_devices(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = _request("GET", "/me/player/devices")
        except requests.RequestException as e:
            return _err(f"spotify list_devices failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        devices = (resp.json() or {}).get("devices") or []
        if not devices:
            return _ok("(no devices visible — open Spotify somewhere first)")
        lines = []
        for d in devices:
            active = "★ active" if d.get("is_active") else "idle"
            lines.append(
                f"- {d.get('name', '?')} ({d.get('type', '?')}, {active})"
                f" [id={d.get('id', '')}]"
            )
        return _ok("\n".join(lines))

    return create_sdk_mcp_server(
        name="spotify",
        version="1.0.0",
        tools=[
            spotify_search,
            spotify_currently_playing,
            spotify_play,
            spotify_pause,
            spotify_queue,
            spotify_list_playlists,
            spotify_list_devices,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "spotify_server is in-process; instantiate via create_spotify_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
