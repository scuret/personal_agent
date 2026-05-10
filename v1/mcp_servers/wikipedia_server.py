"""Wikipedia sub-agent — search + read articles.

Uses Wikipedia's public REST + MediaWiki APIs. No auth required, but a
descriptive User-Agent is mandatory per Wikipedia's API etiquette.

Tools (namespaced as mcp__wikipedia__<name>):

  wiki_search(query, limit?)
      Search article titles. Returns title + snippet + page id.

  wiki_summary(title)
      Short summary of an article (the lead paragraph + thumbnail).
      Cheap; use this first before reaching for the full extract.

  wiki_get_article(title, max_chars?)
      Plain-text extract of the full article, truncated to max_chars
      (default 5000). Use after wiki_summary if more depth is needed.
"""

from __future__ import annotations

from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

API_PHP = "https://en.wikipedia.org/w/api.php"
REST_BASE = "https://en.wikipedia.org/api/rest_v1"
TIMEOUT_S = 12

# Wikipedia requires a descriptive UA per
# https://meta.wikimedia.org/wiki/User-Agent_policy
USER_AGENT = "personal-agent/1.0 (https://github.com/scuret/personal_agent)"


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def create_wikipedia_mcp_server() -> McpSdkServerConfig:
    @tool(
        "wiki_search",
        "Search Wikipedia article titles. Returns up to `limit` matches with snippets.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Default 5.",
                },
            },
            "required": ["query"],
        },
    )
    async def wiki_search(args: dict[str, Any]) -> dict[str, Any]:
        try:
            r = requests.get(
                API_PHP,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": args["query"],
                    "srlimit": int(args.get("limit", 5)),
                    "format": "json",
                    "formatversion": "2",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            return _err(f"wikipedia search failed: {e}")
        results = (r.json() or {}).get("query", {}).get("search", [])
        if not results:
            return _ok("(no matches)")
        lines = []
        for s in results:
            # Snippet contains <span class="searchmatch">...</span> — strip tags.
            snippet = s.get("snippet", "")
            import re
            snippet = re.sub(r"<[^>]+>", "", snippet)
            lines.append(f"- {s.get('title', '?')}\n  {snippet[:200]}")
        return _ok("\n\n".join(lines))

    @tool(
        "wiki_summary",
        "Short summary of a Wikipedia article (lead paragraph). Pass the title — for ambiguous queries use wiki_search first to disambiguate.",
        {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    )
    async def wiki_summary(args: dict[str, Any]) -> dict[str, Any]:
        title = args["title"].replace(" ", "_")
        try:
            r = requests.get(
                f"{REST_BASE}/page/summary/{title}",
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as e:
            return _err(f"wikipedia summary failed: {e}")
        if r.status_code == 404:
            return _err(f"no article found for {args['title']!r}")
        if r.status_code != 200:
            return _err(f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json() or {}
        # Disambiguation pages don't have a clean summary — surface that.
        if d.get("type") == "disambiguation":
            return _ok(
                f"{d.get('title', args['title'])} is a disambiguation page. "
                "Use wiki_search to pick a specific article."
            )
        return _ok(
            f"{d.get('title', '?')}\n"
            f"{d.get('description', '') or '(no description)'}\n"
            f"{d.get('content_urls', {}).get('desktop', {}).get('page', '')}\n\n"
            f"{d.get('extract', '(no extract)')}"
        )

    @tool(
        "wiki_get_article",
        (
            "Plain-text extract of a full Wikipedia article. Use after "
            "wiki_summary if you need more depth. Result is truncated to "
            "max_chars (default 5000)."
        ),
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 50000,
                    "description": "Default 5000.",
                },
            },
            "required": ["title"],
        },
    )
    async def wiki_get_article(args: dict[str, Any]) -> dict[str, Any]:
        max_chars = int(args.get("max_chars", 5000))
        try:
            r = requests.get(
                API_PHP,
                params={
                    "action": "query",
                    "prop": "extracts",
                    "explaintext": "1",
                    "titles": args["title"],
                    "redirects": "1",
                    "format": "json",
                    "formatversion": "2",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_S,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            return _err(f"wikipedia get_article failed: {e}")
        pages = (r.json() or {}).get("query", {}).get("pages", [])
        if not pages:
            return _err("no page returned")
        page = pages[0]
        if page.get("missing"):
            return _err(f"no article found for {args['title']!r}")
        title = page.get("title", args["title"])
        extract = page.get("extract", "") or ""
        if not extract.strip():
            return _err(f"article {title!r} has no plain-text extract available")
        if len(extract) > max_chars:
            extract = extract[:max_chars] + "\n…(truncated)"
        return _ok(f"{title}\n\n{extract}")

    return create_sdk_mcp_server(
        name="wikipedia",
        version="1.0.0",
        tools=[wiki_search, wiki_summary, wiki_get_article],
    )


def main() -> None:
    raise NotImplementedError(
        "wikipedia_server is in-process; instantiate via create_wikipedia_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
