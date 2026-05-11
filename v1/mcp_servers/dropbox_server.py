"""Dropbox sub-agent — search, list, read, share.

Uses Dropbox's HTTP API v2 (https://api.dropboxapi.com / https://content.dropboxapi.com).
Auth uses the OAuth refresh-token flow via `mcp_servers.dropbox_auth`,
which keeps access tokens fresh automatically (4-hour expiry, refreshed
in-process). See dropbox_auth.py for first-time setup.

App setup at dropbox.com/developers/apps:
  1. "Create app" → Scoped access → Full Dropbox (or App folder if you
     prefer to scope tighter).
  2. Permissions tab — at minimum:
       files.metadata.read
       files.content.read
       sharing.read
       sharing.write     (only if you want create_share_link)
  3. Settings tab → copy the App key and App secret into .env as
     DROPBOX_APP_KEY and DROPBOX_APP_SECRET.
  4. Run `python -m mcp_servers.dropbox_auth` once at the Mac to do
     the browser consent and seed the refresh token.

Tools (namespaced as mcp__dropbox__<name>):

  dropbox_search(query, max_results?)
  dropbox_list_folder(path, recursive?)
  dropbox_get_metadata(path)
  dropbox_download_text(path, max_chars?)   — only for small text files
  dropbox_create_share_link(path)
"""

from __future__ import annotations

import json
from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.dropbox_auth import get_access_token

API_BASE = "https://api.dropboxapi.com/2"
CONTENT_BASE = "https://content.dropboxapi.com/2"
TIMEOUT_S = 20


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _token() -> str:
    return get_access_token()


def _post_json(path: str, body: dict[str, Any]) -> Any:
    resp = requests.post(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _normalize_path(p: str) -> str:
    """Dropbox wants paths starting with '/'. Empty string = root."""
    p = (p or "").strip()
    if not p or p == "/":
        return ""
    if not p.startswith("/"):
        return "/" + p
    return p


def _format_entry(e: dict[str, Any]) -> str:
    tag = e.get(".tag", "?")
    name = e.get("name", "?")
    path = e.get("path_display", "")
    if tag == "folder":
        return f"📁 {path or name}"
    size = e.get("size")
    size_str = f" ({size:,} bytes)" if isinstance(size, int) else ""
    return f"📄 {path or name}{size_str}"


def create_dropbox_mcp_server() -> McpSdkServerConfig:
    @tool(
        "dropbox_search",
        "Search Dropbox files and folders by query. Returns up to max_results matches.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Default 20.",
                },
            },
            "required": ["query"],
        },
    )
    async def dropbox_search(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _post_json(
                "/files/search_v2",
                {"query": args["query"], "options": {"max_results": int(args.get("max_results", 20))}},
            )
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"dropbox search failed: {e}")
        matches = data.get("matches") or []
        if not matches:
            return _ok("(no matches)")
        lines = []
        for m in matches:
            md = (m.get("metadata") or {}).get("metadata") or {}
            lines.append(f"- {_format_entry(md)}")
        return _ok("\n".join(lines))

    @tool(
        "dropbox_list_folder",
        (
            "List the contents of a Dropbox folder. Pass '' or '/' for the "
            "root. `recursive=true` walks subfolders too."
        ),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder path. '' or '/' for root."},
                "recursive": {"type": "boolean", "description": "Default false."},
            },
            "required": [],
        },
    )
    async def dropbox_list_folder(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _post_json(
                "/files/list_folder",
                {
                    "path": _normalize_path(args.get("path", "")),
                    "recursive": bool(args.get("recursive", False)),
                },
            )
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"dropbox list_folder failed: {e}")
        entries = data.get("entries") or []
        if not entries:
            return _ok("(empty folder)")
        return _ok("\n".join(f"- {_format_entry(e)}" for e in entries))

    @tool(
        "dropbox_get_metadata",
        "Get metadata for a single file or folder by path.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    async def dropbox_get_metadata(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _post_json("/files/get_metadata", {"path": _normalize_path(args["path"])})
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"dropbox get_metadata failed: {e}")
        return _ok(
            f"name: {data.get('name', '?')}\n"
            f"path: {data.get('path_display', '?')}\n"
            f"type: {data.get('.tag', '?')}\n"
            f"size: {data.get('size', '?')} bytes\n"
            f"client modified: {data.get('client_modified', '?')}\n"
            f"server modified: {data.get('server_modified', '?')}\n"
            f"content_hash: {data.get('content_hash', '?')}"
        )

    @tool(
        "dropbox_download_text",
        (
            "Download a small text file (max 5MB) and return its contents up "
            "to max_chars. For binary files this returns garbled output — "
            "use only for .txt, .md, .csv, source code, etc."
        ),
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 50000,
                    "description": "Default 5000.",
                },
            },
            "required": ["path"],
        },
    )
    async def dropbox_download_text(args: dict[str, Any]) -> dict[str, Any]:
        # Download endpoint takes the path as a JSON-encoded header argument
        # rather than in the body (the body is empty; the file content is
        # the response body).
        try:
            resp = requests.post(
                f"{CONTENT_BASE}/files/download",
                headers={
                    "Authorization": f"Bearer {_token()}",
                    "Dropbox-API-Arg": json.dumps({"path": _normalize_path(args["path"])}),
                },
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"dropbox download failed: {e}")
        if len(resp.content) > 5 * 1024 * 1024:
            return _err(
                f"file too large ({len(resp.content) / 1024 / 1024:.1f}MB; max 5MB)"
            )
        max_chars = int(args.get("max_chars", 5000))
        try:
            text = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            text = resp.content.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(truncated)"
        return _ok(text)

    @tool(
        "dropbox_create_share_link",
        (
            "Create a public shareable link for a file or folder. If a link "
            "already exists, returns the existing one (Dropbox de-duplicates). "
            "Per the personality contract, only call when the principal asks "
            "to share something."
        ),
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    async def dropbox_create_share_link(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _post_json(
                "/sharing/create_shared_link_with_settings",
                {"path": _normalize_path(args["path"]), "settings": {}},
            )
            return _ok(f"share link: {data.get('url', '?')}")
        except requests.RequestException as e:
            # If a link already exists, the API returns 409 with shared_link_already_exists.
            # Fall back to listing existing links.
            if hasattr(e.response, "status_code") and e.response.status_code == 409:
                try:
                    existing = _post_json(
                        "/sharing/list_shared_links",
                        {"path": _normalize_path(args["path"]), "direct_only": True},
                    )
                    links = existing.get("links") or []
                    if links:
                        return _ok(f"existing share link: {links[0].get('url', '?')}")
                except requests.RequestException:
                    pass
            return _err(f"dropbox create_share_link failed: {e}")
        except RuntimeError as e:
            return _err(str(e))

    return create_sdk_mcp_server(
        name="dropbox",
        version="1.0.0",
        tools=[
            dropbox_search,
            dropbox_list_folder,
            dropbox_get_metadata,
            dropbox_download_text,
            dropbox_create_share_link,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "dropbox_server is in-process; instantiate via create_dropbox_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
