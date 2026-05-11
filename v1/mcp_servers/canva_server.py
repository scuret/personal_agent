"""Canva MCP server — list / get / create / export designs + folders.

Uses the Canva Connect API (api.canva.com). Auth flows through
`mcp_servers.canva_auth` (OAuth refresh-token flow).

Tools exposed (namespaced as mcp__canva__<name>):

  canva_list_designs(query?, max_results?)
      List the principal's designs. Optional query narrows by title.
      Returns id, title, thumbnail URL, updated_at for each.

  canva_get_design(design_id)
      Full metadata for a single design: id, title, thumbnail, page
      count, owner, urls.

  canva_create_design(title, design_type?)
      Create a new blank design. design_type controls preset
      dimensions: "presentation", "doc", "instagram_post", etc. Default
      is "presentation".

  canva_export_design(design_id, format?)
      Export a design to PDF / PNG / JPG / PPTX. Polls the export job
      until complete (~10s typical, 30s timeout). Returns the download
      URL.

  canva_list_folders(folder_id?)
      List items in a folder. Omit folder_id for the user's root.

Useful for: searching the principal's design library, surfacing recent
work, kicking off renders, and adding to the morning brief or weekly
review where relevant.
"""

from __future__ import annotations

import time
from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.canva_auth import get_access_token

API_BASE = "https://api.canva.com/rest/v1"
TIMEOUT_S = 20
EXPORT_POLL_INTERVAL_S = 1.5
EXPORT_POLL_TIMEOUT_S = 30


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_access_token()}"}


def _request(method: str, path: str, **kwargs: Any) -> requests.Response:
    headers = kwargs.pop("headers", {}) or {}
    headers.update(_headers())
    return requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=TIMEOUT_S, **kwargs)


def _explain_error(resp: requests.Response) -> str:
    if resp.status_code == 401:
        return "canva auth expired or invalid — token cache may need re-running canva_auth."
    if resp.status_code == 403:
        return "canva forbidden — the requested action isn't covered by the granted scopes."
    if resp.status_code == 404:
        return "canva resource not found (404)."
    if resp.status_code == 429:
        return "canva rate-limited. wait a few seconds and retry."
    return f"canva HTTP {resp.status_code}: {resp.text[:300]}"


def _format_design(d: dict[str, Any]) -> str:
    title = d.get("title") or "(untitled)"
    did = d.get("id") or "?"
    updated = d.get("updated_at") or d.get("created_at") or ""
    thumbnail = ((d.get("thumbnail") or {}).get("url")) or ""
    line = f"- [{did}] {title}"
    if updated:
        line += f" — updated {updated}"
    if thumbnail:
        line += f"\n    thumb: {thumbnail}"
    return line


def create_canva_mcp_server() -> McpSdkServerConfig:
    @tool(
        "canva_list_designs",
        (
            "List the principal's Canva designs. Optional `query` filters "
            "by title (case-insensitive substring on the server side). "
            "Returns id, title, thumbnail URL, and last-updated timestamp."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Filter by title."},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Default 20.",
                },
            },
            "required": [],
        },
    )
    async def canva_list_designs(args: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": int(args.get("max_results", 20))}
        if args.get("query"):
            params["query"] = args["query"]
        try:
            resp = _request("GET", "/designs", params=params)
        except requests.RequestException as e:
            return _err(f"canva list_designs failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        items = (resp.json() or {}).get("items") or []
        if not items:
            return _ok("(no designs)")
        return _ok("\n".join(_format_design(d) for d in items))

    @tool(
        "canva_get_design",
        "Get full metadata for a Canva design by ID.",
        {
            "type": "object",
            "properties": {"design_id": {"type": "string"}},
            "required": ["design_id"],
        },
    )
    async def canva_get_design(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = _request("GET", f"/designs/{args['design_id']}")
        except requests.RequestException as e:
            return _err(f"canva get_design failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        d = (resp.json() or {}).get("design") or resp.json() or {}
        thumb = (d.get("thumbnail") or {}).get("url") or ""
        urls = d.get("urls") or {}
        edit = urls.get("edit_url") or ""
        view = urls.get("view_url") or ""
        text = (
            f"id: {d.get('id', '')}\n"
            f"title: {d.get('title', '')}\n"
            f"page_count: {d.get('page_count', '?')}\n"
            f"created: {d.get('created_at', '')}\n"
            f"updated: {d.get('updated_at', '')}\n"
            f"thumbnail: {thumb}\n"
            f"edit: {edit}\n"
            f"view: {view}"
        )
        return _ok(text)

    @tool(
        "canva_create_design",
        (
            "Create a new blank Canva design. `design_type` controls "
            "preset dimensions — common values: presentation, doc, "
            "instagram_post, instagram_story, pinterest_pin, flyer. "
            "Default 'presentation'."
        ),
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "design_type": {
                    "type": "string",
                    "description": "Canva design type preset. Default 'presentation'.",
                },
            },
            "required": ["title"],
        },
    )
    async def canva_create_design(args: dict[str, Any]) -> dict[str, Any]:
        body = {
            "design_type": {
                "type": "preset",
                "name": args.get("design_type", "presentation"),
            },
            "title": args["title"],
        }
        try:
            resp = _request("POST", "/designs", json=body)
        except requests.RequestException as e:
            return _err(f"canva create_design failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        d = (resp.json() or {}).get("design") or {}
        urls = d.get("urls") or {}
        return _ok(
            f"created design {d.get('id')}\n"
            f"title: {d.get('title')}\n"
            f"edit: {urls.get('edit_url', '')}"
        )

    @tool(
        "canva_export_design",
        (
            "Export a Canva design to a downloadable file. `format` is "
            "one of pdf / png / jpg / pptx (default pdf). Blocks while "
            "the export job runs (~10s typical, 30s cap) and returns "
            "the temporary download URL when ready."
        ),
        {
            "type": "object",
            "properties": {
                "design_id": {"type": "string"},
                "format": {
                    "type": "string",
                    "enum": ["pdf", "png", "jpg", "pptx"],
                    "description": "Default pdf.",
                },
            },
            "required": ["design_id"],
        },
    )
    async def canva_export_design(args: dict[str, Any]) -> dict[str, Any]:
        fmt = args.get("format", "pdf").lower()
        body = {
            "design_id": args["design_id"],
            "format": {"type": fmt},
        }
        try:
            resp = _request("POST", "/exports", json=body)
        except requests.RequestException as e:
            return _err(f"canva export create failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        job = (resp.json() or {}).get("job") or {}
        job_id = job.get("id")
        if not job_id:
            return _err(f"canva export: no job id in response. body: {resp.text[:200]}")

        # Poll until complete (or timeout).
        deadline = time.time() + EXPORT_POLL_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(EXPORT_POLL_INTERVAL_S)
            try:
                poll = _request("GET", f"/exports/{job_id}")
            except requests.RequestException as e:
                return _err(f"canva export poll failed: {e}")
            if not poll.ok:
                return _err(_explain_error(poll))
            j = (poll.json() or {}).get("job") or {}
            status = j.get("status", "")
            if status == "success":
                urls = j.get("urls") or []
                if urls:
                    return _ok(f"exported as {fmt}.\ndownload: {urls[0]}")
                return _ok(f"exported as {fmt}. (no URL in response)")
            if status == "failed":
                err = j.get("error") or {}
                return _err(f"canva export job failed: {err}")
            # else: still in progress, keep polling
        return _err(
            f"canva export timed out after {EXPORT_POLL_TIMEOUT_S}s. "
            f"job_id: {job_id}"
        )

    @tool(
        "canva_list_folders",
        (
            "List items in a Canva folder. Omit folder_id to list the "
            "user's root folder."
        ),
        {
            "type": "object",
            "properties": {
                "folder_id": {"type": "string"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": [],
        },
    )
    async def canva_list_folders(args: dict[str, Any]) -> dict[str, Any]:
        folder_id = args.get("folder_id") or "root"
        params = {"limit": int(args.get("max_results", 20))}
        try:
            resp = _request("GET", f"/folders/{folder_id}/items", params=params)
        except requests.RequestException as e:
            return _err(f"canva list_folders failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        items = (resp.json() or {}).get("items") or []
        if not items:
            return _ok("(empty folder)")
        lines = []
        for it in items:
            kind = it.get("type", "?")
            name = it.get("name") or it.get("title") or "(unnamed)"
            iid = it.get("id") or "?"
            lines.append(f"- [{kind}] {name} (id={iid})")
        return _ok("\n".join(lines))

    return create_sdk_mcp_server(
        name="canva",
        version="1.0.0",
        tools=[
            canva_list_designs,
            canva_get_design,
            canva_create_design,
            canva_export_design,
            canva_list_folders,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "canva_server is in-process; instantiate via create_canva_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
