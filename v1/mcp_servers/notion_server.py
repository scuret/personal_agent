"""Notion MCP server — search, read, query databases, create + append.

Uses Notion's REST API at https://api.notion.com/v1. Auth is a single
bearer token from `NOTION_INTEGRATION_TOKEN` in the env.

⚠️  Notion's permission model is OPT-IN per page. Creating the
integration token isn't enough on its own — you also have to "Connect"
each page or database you want the agent to see. From any page in
Notion: ⋯ menu → Connections → add your integration. Until you do, the
agent will see "no results" for everything in your workspace.

Tools (namespaced as mcp__notion__<name>):

  notion_search(query, page_size?)
      Find pages and databases the integration has access to. Returns
      id + title + type + last edited.

  notion_get_page(page_id)
      Read a page: title + body content (top-level blocks rendered as
      light Markdown). Nested blocks are not recursively expanded — keeps
      output bounded; v2 problem if the agent asks for it.

  notion_query_database(database_id, page_size?)
      List rows in a database with their visible properties.

  notion_create_page(parent_page_id, title, body_text?)
      Create a child page under a parent page. Per the personality's
      "ask before modifying" rule, only call when the principal asks.

  notion_append_text(page_id, text)
      Append a paragraph to an existing page.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers._untrusted import wrap_untrusted

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TIMEOUT_S = 15


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _headers() -> dict[str, str]:
    token = os.environ.get("NOTION_INTEGRATION_TOKEN", "").strip()
    if not token:
        raise RuntimeError("NOTION_INTEGRATION_TOKEN not set in .env")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# ─── Text extraction helpers ────────────────────────────────────────────────


def _rich_text_to_str(rich: list[dict[str, Any]] | None) -> str:
    if not rich:
        return ""
    return "".join(seg.get("plain_text", "") for seg in rich if isinstance(seg, dict))


def _title_of(obj: dict[str, Any]) -> str:
    """Extract a usable title from a Notion page or database object."""
    # Pages: properties.<title prop>.title is the rich-text array
    props = obj.get("properties") or {}
    for p in props.values():
        if isinstance(p, dict) and p.get("type") == "title":
            return _rich_text_to_str(p.get("title")) or "(untitled)"
    # Databases: title is at the top level
    if "title" in obj:
        return _rich_text_to_str(obj.get("title")) or "(untitled)"
    return "(untitled)"


def _block_to_markdown(block: dict[str, Any]) -> str | None:
    """Render one Notion block as light Markdown. Skips media/embed blocks.

    Doesn't recurse into nested children — keeps output bounded so a deeply
    nested page doesn't blow up the agent's context. The agent can request
    a child block separately if needed (out of scope for v1).
    """
    btype = block.get("type")
    if not btype:
        return None
    payload = block.get(btype) or {}
    if not isinstance(payload, dict):
        return None
    text = _rich_text_to_str(payload.get("rich_text"))

    if btype == "paragraph":
        return text or None
    if btype == "heading_1":
        return f"# {text}" if text else None
    if btype == "heading_2":
        return f"## {text}" if text else None
    if btype == "heading_3":
        return f"### {text}" if text else None
    if btype == "bulleted_list_item":
        return f"- {text}"
    if btype == "numbered_list_item":
        return f"1. {text}"  # we don't track index across siblings
    if btype == "to_do":
        check = "x" if payload.get("checked") else " "
        return f"- [{check}] {text}"
    if btype == "quote":
        return f"> {text}"
    if btype == "callout":
        return f"💡 {text}"
    if btype == "code":
        lang = payload.get("language", "")
        return f"```{lang}\n{text}\n```"
    if btype == "divider":
        return "---"
    return None  # image / video / file / embed / table / etc.


def create_notion_mcp_server() -> McpSdkServerConfig:
    @tool(
        "notion_search",
        (
            "Search Notion pages and databases the integration has access to. "
            "Returns id + title + type + last edited. If results look empty "
            "but you expected matches, the page probably hasn't been shared "
            "with the integration yet (Notion permission is opt-in per page)."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Default 20.",
                },
            },
            "required": ["query"],
        },
    )
    async def notion_search(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.post(
                f"{API_BASE}/search",
                headers=_headers(),
                json={
                    "query": args["query"],
                    "page_size": int(args.get("page_size", 20)),
                },
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"notion search failed: {e}")
        items = resp.json().get("results", [])
        if not items:
            return _ok(
                "no results. if you expected matches, check that the page is "
                "shared with the integration (page → ⋯ → connections)."
            )
        lines = []
        for it in items:
            obj_type = it.get("object", "?")
            iid = it.get("id", "?")
            title = _title_of(it)
            edited = it.get("last_edited_time", "")[:10]
            lines.append(f"- [{obj_type}] {title} (id={iid}, edited {edited})")
        return _ok("\n".join(lines))

    @tool(
        "notion_get_page",
        (
            "Read a Notion page: title plus body content as light Markdown. "
            "Top-level blocks only — nested children aren't recursed."
        ),
        {
            "type": "object",
            "properties": {"page_id": {"type": "string"}},
            "required": ["page_id"],
        },
    )
    async def notion_get_page(args: dict[str, Any]) -> dict[str, Any]:
        page_id = args["page_id"]
        try:
            page_resp = requests.get(
                f"{API_BASE}/pages/{page_id}", headers=_headers(), timeout=TIMEOUT_S
            )
            page_resp.raise_for_status()
            blocks_resp = requests.get(
                f"{API_BASE}/blocks/{page_id}/children",
                headers=_headers(),
                params={"page_size": 100},
                timeout=TIMEOUT_S,
            )
            blocks_resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"notion get_page failed: {e}")

        page = page_resp.json()
        title = _title_of(page)
        rendered: list[str] = []
        for b in blocks_resp.json().get("results", []):
            md = _block_to_markdown(b)
            if md is not None:
                rendered.append(md)
        body = "\n".join(rendered) if rendered else "(empty page or all blocks are media/unsupported)"
        # Notion page contents may have been authored by anyone the
        # workspace was shared with — treat as untrusted.
        return _ok(wrap_untrusted(
            f"Notion page {title!r} ({page_id})",
            f"# {title}\n\n{body}",
        ))

    @tool(
        "notion_query_database",
        "List rows in a Notion database. Returns id + title (or first text property) per row.",
        {
            "type": "object",
            "properties": {
                "database_id": {"type": "string"},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["database_id"],
        },
    )
    async def notion_query_database(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.post(
                f"{API_BASE}/databases/{args['database_id']}/query",
                headers=_headers(),
                json={"page_size": int(args.get("page_size", 25))},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"notion query_database failed: {e}")
        rows = resp.json().get("results", [])
        if not rows:
            return _ok("no rows.")
        lines = []
        for r in rows:
            title = _title_of(r)
            lines.append(f"- [{r.get('id', '?')}] {title}")
        return _ok("\n".join(lines))

    @tool(
        "notion_create_page",
        (
            "Create a new child page under a parent Notion page. Per the "
            "personality contract, only call when the principal asks. "
            "Database parents aren't supported in v1 (schema-dependent)."
        ),
        {
            "type": "object",
            "properties": {
                "parent_page_id": {"type": "string"},
                "title": {"type": "string"},
                "body_text": {
                    "type": "string",
                    "description": "Optional initial paragraph content. Multi-line strings split into paragraphs.",
                },
            },
            "required": ["parent_page_id", "title"],
        },
    )
    async def notion_create_page(args: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parent": {"page_id": args["parent_page_id"]},
            "properties": {
                "title": {"title": [{"text": {"content": args["title"]}}]}
            },
        }
        body_text = args.get("body_text")
        if body_text:
            paragraphs = [p for p in body_text.split("\n\n") if p.strip()]
            body["children"] = [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": p}}]
                    },
                }
                for p in paragraphs
            ]
        try:
            resp = requests.post(
                f"{API_BASE}/pages",
                headers=_headers(),
                json=body,
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"notion create_page failed: {e}")
        page = resp.json()
        return _ok(f"created [{page.get('id', '?')}]: {args['title']}\n{page.get('url', '')}")

    @tool(
        "notion_append_text",
        "Append a paragraph (or paragraphs, split on double-newline) to an existing Notion page.",
        {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["page_id", "text"],
        },
    )
    async def notion_append_text(args: dict[str, Any]) -> dict[str, Any]:
        paragraphs = [p for p in args["text"].split("\n\n") if p.strip()]
        if not paragraphs:
            return _err("no text to append.")
        children = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": p}}]
                },
            }
            for p in paragraphs
        ]
        try:
            resp = requests.patch(
                f"{API_BASE}/blocks/{args['page_id']}/children",
                headers=_headers(),
                json={"children": children},
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"notion append_text failed: {e}")
        return _ok(f"appended {len(paragraphs)} paragraph(s) to {args['page_id']}")

    return create_sdk_mcp_server(
        name="notion",
        version="1.0.0",
        tools=[
            notion_search,
            notion_get_page,
            notion_query_database,
            notion_create_page,
            notion_append_text,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "notion_server is in-process; instantiate via create_notion_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
