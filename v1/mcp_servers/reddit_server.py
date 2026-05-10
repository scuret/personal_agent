"""Reddit sub-agent — public read-only.

Uses Reddit's public JSON endpoints (no OAuth required). Append `.json`
to any reddit URL and it returns structured data. A descriptive
User-Agent is required — Reddit aggressively rate-limits / 429s
generic UAs.

Authenticated mode would unlock higher rate limits + private subs but
needs an OAuth "script" app and is out of scope for v1.

Tools (namespaced as mcp__reddit__<name>):

  reddit_subreddit_top(subreddit, time_range?, limit?)
      Top posts in r/<subreddit>. time_range ∈ {hour, day, week, month,
      year, all}. Default 'day'.

  reddit_subreddit_hot(subreddit, limit?)
      Currently hot posts in r/<subreddit>.

  reddit_search(query, subreddit?, limit?)
      Search posts site-wide or scoped to a subreddit.

  reddit_get_post(subreddit, post_id, comment_limit?)
      Fetch a post + top-N comments. post_id is the short ID like
      "abc123" (the bit between /comments/ and the slug in the URL).
"""

from __future__ import annotations

from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

REDDIT_BASE = "https://www.reddit.com"
TIMEOUT_S = 12

# Reddit aggressively 429s generic UAs. This needs to be descriptive.
USER_AGENT = "personal-agent:v1.0 (by /u/personal_agent_bot)"


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _format_post(p: dict[str, Any]) -> str:
    """Format one Reddit post (data dict) as a single readable block."""
    title = p.get("title", "(no title)")
    author = p.get("author", "?")
    sub = p.get("subreddit_name_prefixed", "")
    score = p.get("score", 0)
    num_comments = p.get("num_comments", 0)
    pid = p.get("id", "?")
    permalink = p.get("permalink", "")
    url = (
        p.get("url_overridden_by_dest")
        or p.get("url")
        or (f"https://reddit.com{permalink}" if permalink else "")
    )
    selftext = (p.get("selftext") or "").strip()
    block = (
        f"[{pid}] {title}\n"
        f"  {sub} · u/{author} · ↑{score} · {num_comments} comments\n"
        f"  {url}"
    )
    if selftext:
        snippet = selftext[:300]
        if len(selftext) > 300:
            snippet += "…"
        block += f"\n  {snippet}"
    return block


def _format_comment(c: dict[str, Any]) -> str:
    author = c.get("author", "?")
    score = c.get("score", 0)
    body = (c.get("body") or "").strip()
    if len(body) > 400:
        body = body[:400] + "…"
    return f"  u/{author} · ↑{score}: {body}"


def _children(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull `data.children[*].data` from a Reddit listing response."""
    children = (payload.get("data") or {}).get("children") or []
    return [c.get("data", {}) for c in children if isinstance(c, dict)]


def _get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    r = requests.get(
        f"{REDDIT_BASE}{path}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        params=params,
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()


def create_reddit_mcp_server() -> McpSdkServerConfig:
    @tool(
        "reddit_subreddit_top",
        (
            "Top posts in a subreddit. time_range is hour/day/week/month/"
            "year/all (default day). Returns title, score, comments, URL, "
            "and a short selftext snippet for self-posts."
        ),
        {
            "type": "object",
            "properties": {
                "subreddit": {
                    "type": "string",
                    "description": "Subreddit name without 'r/'. e.g. 'localllama'",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["hour", "day", "week", "month", "year", "all"],
                    "description": "Default 'day'.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Default 10.",
                },
            },
            "required": ["subreddit"],
        },
    )
    async def reddit_subreddit_top(args: dict[str, Any]) -> dict[str, Any]:
        sub = args["subreddit"].lstrip("r/").strip()
        try:
            data = _get_json(
                f"/r/{sub}/top.json",
                {"t": args.get("time_range", "day"), "limit": int(args.get("limit", 10))},
            )
        except requests.RequestException as e:
            return _err(f"reddit subreddit_top failed: {e}")
        posts = _children(data)
        if not posts:
            return _ok(f"(no posts in r/{sub} for that time range)")
        return _ok("\n\n".join(_format_post(p) for p in posts))

    @tool(
        "reddit_subreddit_hot",
        "Currently hot posts in a subreddit.",
        {
            "type": "object",
            "properties": {
                "subreddit": {"type": "string", "description": "Without 'r/' prefix."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Default 10."},
            },
            "required": ["subreddit"],
        },
    )
    async def reddit_subreddit_hot(args: dict[str, Any]) -> dict[str, Any]:
        sub = args["subreddit"].lstrip("r/").strip()
        try:
            data = _get_json(
                f"/r/{sub}/hot.json",
                {"limit": int(args.get("limit", 10))},
            )
        except requests.RequestException as e:
            return _err(f"reddit subreddit_hot failed: {e}")
        posts = _children(data)
        if not posts:
            return _ok(f"(no hot posts in r/{sub})")
        return _ok("\n\n".join(_format_post(p) for p in posts))

    @tool(
        "reddit_search",
        (
            "Search Reddit posts. Pass `subreddit` to scope the search; "
            "leave it off to search site-wide."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "subreddit": {"type": "string", "description": "Optional. Without 'r/' prefix."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Default 10."},
            },
            "required": ["query"],
        },
    )
    async def reddit_search(args: dict[str, Any]) -> dict[str, Any]:
        params = {"q": args["query"], "limit": int(args.get("limit", 10)), "sort": "relevance"}
        sub = (args.get("subreddit") or "").lstrip("r/").strip()
        if sub:
            params["restrict_sr"] = "on"
            path = f"/r/{sub}/search.json"
        else:
            path = "/search.json"
        try:
            data = _get_json(path, params)
        except requests.RequestException as e:
            return _err(f"reddit search failed: {e}")
        posts = _children(data)
        if not posts:
            return _ok("(no matches)")
        return _ok("\n\n".join(_format_post(p) for p in posts))

    @tool(
        "reddit_get_post",
        (
            "Fetch a Reddit post + its top comments. `post_id` is the "
            "short ID between /comments/ and the slug in the URL "
            "(e.g. 'abc123')."
        ),
        {
            "type": "object",
            "properties": {
                "subreddit": {"type": "string"},
                "post_id": {"type": "string"},
                "comment_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Default 10.",
                },
            },
            "required": ["subreddit", "post_id"],
        },
    )
    async def reddit_get_post(args: dict[str, Any]) -> dict[str, Any]:
        sub = args["subreddit"].lstrip("r/").strip()
        pid = args["post_id"].strip()
        try:
            data = _get_json(
                f"/r/{sub}/comments/{pid}.json",
                {"limit": int(args.get("comment_limit", 10)), "sort": "top"},
            )
        except requests.RequestException as e:
            return _err(f"reddit get_post failed: {e}")
        # Reddit returns a 2-element array: [post_listing, comments_listing]
        if not isinstance(data, list) or len(data) < 2:
            return _err("unexpected response shape")
        post_children = _children(data[0])
        if not post_children:
            return _err(f"post {pid} not found in r/{sub}")
        post = post_children[0]
        comments = _children(data[1])
        # Filter out non-comment items (e.g. "more" stubs).
        comments = [c for c in comments if c.get("body")]
        body_block = _format_post(post)
        if not comments:
            return _ok(body_block + "\n\n(no comments yet)")
        comment_lines = "\n".join(_format_comment(c) for c in comments)
        return _ok(f"{body_block}\n\n--- top comments ---\n{comment_lines}")

    return create_sdk_mcp_server(
        name="reddit",
        version="1.0.0",
        tools=[
            reddit_subreddit_top,
            reddit_subreddit_hot,
            reddit_search,
            reddit_get_post,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "reddit_server is in-process; instantiate via create_reddit_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
