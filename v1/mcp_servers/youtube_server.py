"""YouTube Data API v3 sub-agent — public read only.

Uses a Google Cloud API key (NOT your OAuth credentials.json). Get one
at console.cloud.google.com → enable "YouTube Data API v3" on the
project → Credentials → "Create credentials" → "API key" → optionally
restrict to the YouTube Data API.

Tools (namespaced as mcp__youtube__<name>):

  youtube_search(query, max_results?)
      Find videos by query. Returns video_id + title + channel + snippet.

  youtube_get_video(video_id)
      Title, description, channel, view/like counts, duration.

  youtube_get_channel(channel_id_or_handle)
      Title, description, subscriber count, video count. Accepts
      "UC..." channel IDs or "@handle" strings.

  youtube_list_channel_uploads(channel_id, max_results?)
      Recent uploads from a channel.

Free quota: 10K units/day. Search costs 100 units, video/channel
lookups cost 1. Calling search 100 times/day exhausts the quota — use
sparingly, or restrict to specific channels you care about.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

API_BASE = "https://www.googleapis.com/youtube/v3"
TIMEOUT_S = 15


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _key() -> str:
    k = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not k:
        raise RuntimeError("YOUTUBE_API_KEY not set in .env")
    return k


def _resolve_channel_id(handle_or_id: str) -> str | None:
    """Accept '@handle' or 'UC...' and return a UC-style channel ID."""
    if handle_or_id.startswith("UC") and len(handle_or_id) == 24:
        return handle_or_id
    handle = handle_or_id.lstrip("@").strip()
    if not handle:
        return None
    try:
        resp = requests.get(
            f"{API_BASE}/channels",
            params={"part": "id", "forHandle": f"@{handle}", "key": _key()},
            timeout=TIMEOUT_S,
        )
        resp.raise_for_status()
        items = (resp.json() or {}).get("items") or []
        return items[0]["id"] if items else None
    except (requests.RequestException, RuntimeError, KeyError):
        return None


def _format_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "?"


def create_youtube_mcp_server() -> McpSdkServerConfig:
    @tool(
        "youtube_search",
        (
            "Search YouTube for videos by query. Returns video_id + title + "
            "channel + first ~150 chars of description. Costs 100 quota "
            "units per call so use only when the principal asks for video "
            "discovery — not as a casual lookup."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Default 5.",
                },
            },
            "required": ["query"],
        },
    )
    async def youtube_search(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.get(
                f"{API_BASE}/search",
                params={
                    "part": "snippet",
                    "type": "video",
                    "q": args["query"],
                    "maxResults": int(args.get("max_results", 5)),
                    "key": _key(),
                },
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"youtube search failed: {e}")
        items = (resp.json() or {}).get("items") or []
        if not items:
            return _ok("(no videos found)")
        lines = []
        for it in items:
            sn = it.get("snippet") or {}
            vid = (it.get("id") or {}).get("videoId", "?")
            title = sn.get("title", "(no title)")
            ch = sn.get("channelTitle", "?")
            desc = (sn.get("description") or "")[:150]
            lines.append(f"- [{vid}] {title}\n  by {ch}\n  {desc}")
        return _ok("\n\n".join(lines))

    @tool(
        "youtube_get_video",
        "Get full metadata for one YouTube video by ID — title, channel, view/like counts, duration, full description.",
        {
            "type": "object",
            "properties": {"video_id": {"type": "string"}},
            "required": ["video_id"],
        },
    )
    async def youtube_get_video(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.get(
                f"{API_BASE}/videos",
                params={
                    "id": args["video_id"],
                    "part": "snippet,statistics,contentDetails",
                    "key": _key(),
                },
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"youtube get_video failed: {e}")
        items = (resp.json() or {}).get("items") or []
        if not items:
            return _err(f"no video found for id {args['video_id']!r}")
        v = items[0]
        sn = v.get("snippet") or {}
        st = v.get("statistics") or {}
        cd = v.get("contentDetails") or {}
        desc = sn.get("description") or ""
        if len(desc) > 1500:
            desc = desc[:1500] + "…"
        return _ok(
            f"{sn.get('title', '?')}\n"
            f"by {sn.get('channelTitle', '?')} | duration {cd.get('duration', '?')}\n"
            f"views: {_format_int(st.get('viewCount'))} | "
            f"likes: {_format_int(st.get('likeCount'))} | "
            f"comments: {_format_int(st.get('commentCount'))}\n"
            f"published: {sn.get('publishedAt', '?')}\n"
            f"https://youtube.com/watch?v={v.get('id', '')}\n\n"
            f"{desc}"
        )

    @tool(
        "youtube_get_channel",
        (
            "Get info about a channel. `channel_id_or_handle` accepts a "
            "'UC...' channel ID or '@handle' (e.g. '@mkbhd'). Returns "
            "title, description, subscriber count, total video count."
        ),
        {
            "type": "object",
            "properties": {"channel_id_or_handle": {"type": "string"}},
            "required": ["channel_id_or_handle"],
        },
    )
    async def youtube_get_channel(args: dict[str, Any]) -> dict[str, Any]:
        cid = _resolve_channel_id(args["channel_id_or_handle"])
        if not cid:
            return _err(f"couldn't resolve {args['channel_id_or_handle']!r} to a channel id")
        try:
            resp = requests.get(
                f"{API_BASE}/channels",
                params={"id": cid, "part": "snippet,statistics", "key": _key()},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"youtube get_channel failed: {e}")
        items = (resp.json() or {}).get("items") or []
        if not items:
            return _err(f"no channel found for {cid}")
        c = items[0]
        sn = c.get("snippet") or {}
        st = c.get("statistics") or {}
        return _ok(
            f"{sn.get('title', '?')} ({cid})\n"
            f"https://youtube.com/channel/{cid}\n"
            f"subs: {_format_int(st.get('subscriberCount'))} | "
            f"videos: {_format_int(st.get('videoCount'))} | "
            f"views: {_format_int(st.get('viewCount'))}\n\n"
            f"{(sn.get('description') or '')[:1000]}"
        )

    @tool(
        "youtube_list_channel_uploads",
        "List the most recent uploads from a YouTube channel.",
        {
            "type": "object",
            "properties": {
                "channel_id_or_handle": {"type": "string"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Default 10.",
                },
            },
            "required": ["channel_id_or_handle"],
        },
    )
    async def youtube_list_channel_uploads(args: dict[str, Any]) -> dict[str, Any]:
        cid = _resolve_channel_id(args["channel_id_or_handle"])
        if not cid:
            return _err(f"couldn't resolve {args['channel_id_or_handle']!r} to a channel id")
        # Channel uploads playlist id is the channel id with the second char swapped to 'U'.
        # E.g. UCxxxx → UUxxxx.
        uploads_pl = "UU" + cid[2:]
        try:
            resp = requests.get(
                f"{API_BASE}/playlistItems",
                params={
                    "playlistId": uploads_pl,
                    "part": "snippet",
                    "maxResults": int(args.get("max_results", 10)),
                    "key": _key(),
                },
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"youtube list_channel_uploads failed: {e}")
        items = (resp.json() or {}).get("items") or []
        if not items:
            return _ok("(no uploads found)")
        lines = []
        for it in items:
            sn = it.get("snippet") or {}
            vid = (sn.get("resourceId") or {}).get("videoId", "?")
            title = sn.get("title", "(no title)")
            published = (sn.get("publishedAt") or "")[:10]
            lines.append(f"- [{vid}] {published}: {title}")
        return _ok("\n".join(lines))

    return create_sdk_mcp_server(
        name="youtube",
        version="1.0.0",
        tools=[
            youtube_search,
            youtube_get_video,
            youtube_get_channel,
            youtube_list_channel_uploads,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "youtube_server is in-process; instantiate via create_youtube_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
