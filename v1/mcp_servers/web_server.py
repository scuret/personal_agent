"""Web search + fetch MCP server.

Closes the biggest gap in v1: the agent had no way to answer factual
questions or look up current events. Two tools:

  web_search(query, count?)
      Brave Search API. Returns title + url + snippet for each result.
      Free tier is 2K queries/month at 1 q/s.
      Auth via BRAVE_SEARCH_API_KEY in env.

  web_fetch(url, max_chars?)
      Fetch a single URL, strip HTML to plain text, return up to
      max_chars (default 5000). Skips script/style/nav/header/footer
      tags so the result is mostly readable prose. No JavaScript
      execution — pages that require JS to render won't have useful
      content.

Typical agent flow: user asks "what's the latest on X" → agent calls
web_search → looks at snippets → if it needs more depth, calls
web_fetch on a specific URL → summarizes → replies.
"""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers._untrusted import wrap_untrusted

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
TIMEOUT_S = 15
USER_AGENT = "personal-agent/1.0 (+https://github.com/scuret/personal_agent)"


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


# ─── HTML to text ────────────────────────────────────────────────────────────


class _TextExtractor(HTMLParser):
    """Minimal HTML→text. Skips script/style/nav/header/footer/aside content
    so the output is mostly the page's prose."""

    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # collapse whitespace within lines, preserve paragraph breaks
        lines = [re.sub(r"[ \t\r\f]+", " ", ln).strip() for ln in raw.split("\n")]
        return "\n".join(ln for ln in lines if ln)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — bad HTML shouldn't crash the daemon
        pass
    return parser.text()


# ─── Tools ──────────────────────────────────────────────────────────────────


def create_web_mcp_server() -> McpSdkServerConfig:
    @tool(
        "web_search",
        (
            "Search the web for information you don't have via other tools. "
            "Returns title + URL + snippet for each result. Use for factual "
            "questions, current events, lookups outside Gmail/Calendar/"
            "Todoist/Notion/GitHub. If the snippets aren't enough, follow up "
            "with web_fetch on a specific URL."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Max results. Default 5.",
                },
            },
            "required": ["query"],
        },
    )
    async def web_search(args: dict[str, Any]) -> dict[str, Any]:
        key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        if not key:
            return _err("BRAVE_SEARCH_API_KEY not set in .env")
        try:
            resp = requests.get(
                BRAVE_SEARCH_URL,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": key,
                    "User-Agent": USER_AGENT,
                },
                params={"q": args["query"], "count": int(args.get("count", 5))},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"web search failed: {e}")
        web = (resp.json() or {}).get("web") or {}
        results = web.get("results") or []
        if not results:
            return _ok("(no results)")
        lines = []
        for r in results:
            title = (r.get("title") or "").strip()
            url = r.get("url") or ""
            desc = (r.get("description") or "").strip()
            # Brave includes <strong>...</strong> highlights; strip them.
            desc = re.sub(r"</?strong>", "", desc)
            lines.append(f"- {title}\n  {url}\n  {desc[:240]}")
        # Search-result snippets are arbitrary web content — could
        # contain instruction-shaped lures from result-ranking spam.
        return _ok(wrap_untrusted(
            f"Brave web search results for query={args['query']!r}",
            "\n\n".join(lines),
        ))

    @tool(
        "web_fetch",
        (
            "Fetch a URL, strip HTML, return plain-text content. Use when "
            "web_search snippets aren't enough and you need the article body. "
            "No JavaScript execution — pages that require JS won't render "
            "useful content. Result is truncated to max_chars (default 5000)."
        ),
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL including https://"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 20000,
                    "description": "Max characters of body text to return. Default 5000.",
                },
            },
            "required": ["url"],
        },
    )
    async def web_fetch(args: dict[str, Any]) -> dict[str, Any]:
        url = args["url"]
        max_chars = int(args.get("max_chars", 5000))
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=TIMEOUT_S,
                allow_redirects=True,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"fetch failed: {e}")
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype.lower() and "xml" not in ctype.lower():
            # For plain text / markdown / json, just return raw (truncated).
            body = resp.text[:max_chars]
            return _ok(wrap_untrusted(
                f"web page at {url} (content-type: {ctype})", body
            ))
        text = _html_to_text(resp.text)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(truncated)"
        return _ok(wrap_untrusted(
            f"web page at {url} (status {resp.status_code})", text
        ))

    return create_sdk_mcp_server(
        name="web",
        version="1.0.0",
        tools=[web_search, web_fetch],
    )


def main() -> None:
    raise NotImplementedError(
        "web_server is in-process; instantiate via create_web_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
