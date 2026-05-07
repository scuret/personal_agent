"""Todoist MCP server — list/create/update/complete tasks, list projects & labels.

Uses Todoist's REST API v1 at https://api.todoist.com/api/v1 (the older
/rest/v2 base was retired and now returns 410 Gone). Auth is a single
bearer token from `TODOIST_API_KEY` in the env. No OAuth.

Tools exposed (namespaced as mcp__todoist__<name>):

  todoist_list_tasks(filter?, project_id?, limit?)
      Filter is Todoist's query syntax: `today`, `overdue`, `7 days`,
      `p1`, `@home`, etc. Combine with & and |. See
      https://todoist.com/help/articles/introduction-to-filters

  todoist_create_task(content, due_string?, priority?, project_id?,
                      labels?, description?)
      Priority is 1-4 in the API where 4=urgent (Todoist's UI inverts
      this — "P1 urgent" maps to API priority=4). The tool description
      below tells the agent so.

  todoist_update_task(task_id, content?, due_string?, priority?, labels?)
  todoist_complete_task(task_id)
  todoist_list_projects()  — projects with their IDs, for filter shortcuts
  todoist_list_labels()    — labels with their IDs

Per project decision: the agent discovers projects/labels via the list
tools rather than hardcoding any IDs. Works for any Todoist setup.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

API_BASE = "https://api.todoist.com/api/v1"


def _headers() -> dict[str, str]:
    api_key = os.environ.get("TODOIST_API_KEY", "")
    if not api_key:
        raise RuntimeError("TODOIST_API_KEY is not set in the environment")
    return {"Authorization": f"Bearer {api_key}"}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _unwrap_list(payload: Any) -> list[Any]:
    """Todoist v1 wraps list endpoints as {'results': [...], 'next_cursor': ...}.

    Defensive: also accept a bare list in case an endpoint isn't wrapped.
    Pagination via next_cursor is not implemented — first page is plenty
    for personal-scale data.
    """
    if isinstance(payload, dict) and "results" in payload:
        return list(payload["results"])
    if isinstance(payload, list):
        return payload
    return []


def _format_task(t: dict[str, Any]) -> str:
    pri_map = {4: "p1", 3: "p2", 2: "p3", 1: "p4"}
    pri = pri_map.get(t.get("priority", 1), "p?")
    parts = [f"[{t['id']}]", pri, t.get("content", "")]
    due = (t.get("due") or {}).get("string")
    if due:
        parts.append(f"(due {due})")
    if t.get("labels"):
        parts.append(f"@{','.join(t['labels'])}")
    return " ".join(parts)


def create_todoist_mcp_server() -> McpSdkServerConfig:
    list_tasks_schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": (
                    "Todoist filter query: 'today', 'overdue', '7 days', "
                    "'p1' (urgent), '@home', or boolean combos like "
                    "'today & p1'. Omit to list everything in inbox."
                ),
            },
            "project_id": {
                "type": "string",
                "description": "Restrict to one project. Use todoist_list_projects to find IDs.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Max tasks to return. Default 30.",
            },
        },
        "required": [],
    }

    @tool(
        "todoist_list_tasks",
        (
            "List active Todoist tasks with optional filter and project. Use "
            "this for 'what's due today', 'show me my overdue stuff', etc. "
            "The filter param accepts Todoist's filter syntax."
        ),
        list_tasks_schema,
    )
    async def todoist_list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if args.get("filter"):
            params["filter"] = args["filter"]
        if args.get("project_id"):
            params["project_id"] = args["project_id"]
        try:
            resp = requests.get(f"{API_BASE}/tasks", headers=_headers(), params=params, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"todoist list_tasks failed: {e}")
        tasks = _unwrap_list(resp.json())
        limit = int(args.get("limit", 30))
        if not tasks:
            return _ok("no matching tasks.")
        body = "\n".join(_format_task(t) for t in tasks[:limit])
        if len(tasks) > limit:
            body += f"\n…+{len(tasks) - limit} more"
        return _ok(body)

    create_task_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Task title (required)."},
            "due_string": {
                "type": "string",
                "description": (
                    "Natural-language due date: 'tomorrow at 3pm', 'next "
                    "Monday', 'in 2 weeks'. Todoist parses it."
                ),
            },
            "priority": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "description": (
                    "API priority 1-4. 4 = urgent (Todoist UI 'P1'), "
                    "3 = high (UI 'P2'), 2 = medium (UI 'P3'), "
                    "1 = normal (UI 'P4', default). Map user words: "
                    "'urgent'->4, 'high'->3, 'medium'->2, 'low/normal'->1."
                ),
            },
            "project_id": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "description": {
                "type": "string",
                "description": "Longer description body, optional.",
            },
        },
        "required": ["content"],
    }

    @tool(
        "todoist_create_task",
        "Create a new Todoist task. Returns the created task's ID and content.",
        create_task_schema,
    )
    async def todoist_create_task(args: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"content": args["content"]}
        for k in ("due_string", "priority", "project_id", "labels", "description"):
            if args.get(k) is not None:
                body[k] = args[k]
        try:
            resp = requests.post(
                f"{API_BASE}/tasks",
                headers={**_headers(), "Content-Type": "application/json"},
                data=json.dumps(body),
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"todoist create_task failed: {e}")
        t = resp.json()
        return _ok(f"created [{t['id']}]: {t.get('content', '')}")

    update_task_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "content": {"type": "string"},
            "due_string": {"type": "string", "description": "Pass empty string to clear the due date."},
            "priority": {"type": "integer", "minimum": 1, "maximum": 4},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["task_id"],
    }

    @tool(
        "todoist_update_task",
        "Update an existing Todoist task's fields. Only the fields you pass are changed.",
        update_task_schema,
    )
    async def todoist_update_task(args: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for k in ("content", "due_string", "priority", "labels"):
            if args.get(k) is not None:
                body[k] = args[k]
        if not body:
            return _err("nothing to update — pass at least one field.")
        try:
            resp = requests.post(
                f"{API_BASE}/tasks/{args['task_id']}",
                headers={**_headers(), "Content-Type": "application/json"},
                data=json.dumps(body),
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"todoist update_task failed: {e}")
        return _ok(f"updated [{args['task_id']}]")

    @tool(
        "todoist_complete_task",
        "Mark a Todoist task complete by ID. Use only when the principal asks you to.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    )
    async def todoist_complete_task(args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.post(
                f"{API_BASE}/tasks/{args['task_id']}/close", headers=_headers(), timeout=15
            )
        except requests.RequestException as e:
            return _err(f"todoist complete_task failed: {e}")
        if resp.status_code == 204:
            return _ok(f"completed [{args['task_id']}]")
        return _err(f"todoist complete_task returned {resp.status_code}: {resp.text[:200]}")

    @tool(
        "todoist_list_projects",
        (
            "List all Todoist projects with their IDs. Call this once to "
            "discover the principal's project structure when you need to "
            "filter or create tasks in a specific project."
        ),
        {"type": "object", "properties": {}, "required": []},
    )
    async def todoist_list_projects(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.get(f"{API_BASE}/projects", headers=_headers(), timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"todoist list_projects failed: {e}")
        projects = _unwrap_list(resp.json())
        if not projects:
            return _ok("no projects (just inbox).")
        lines = [f"- [{p['id']}] {p['name']}" for p in projects]
        return _ok("\n".join(lines))

    @tool(
        "todoist_list_labels",
        "List all Todoist labels with their IDs.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def todoist_list_labels(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.get(f"{API_BASE}/labels", headers=_headers(), timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"todoist list_labels failed: {e}")
        labels = _unwrap_list(resp.json())
        if not labels:
            return _ok("no labels defined.")
        lines = [f"- [{lbl['id']}] {lbl['name']}" for lbl in labels]
        return _ok("\n".join(lines))

    return create_sdk_mcp_server(
        name="todoist",
        version="1.0.0",
        tools=[
            todoist_list_tasks,
            todoist_create_task,
            todoist_update_task,
            todoist_complete_task,
            todoist_list_projects,
            todoist_list_labels,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "todoist_server is in-process; instantiate via create_todoist_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
