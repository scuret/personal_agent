"""GitHub MCP server — repos, issues, PRs, commits, search.

Uses GitHub's REST API v3 at https://api.github.com. Auth is a personal
access token from `GITHUB_TOKEN` in the env. Either classic or
fine-grained tokens work; the Bearer scheme covers both.

Token setup (do this on the web — github.com/settings/tokens):
  - Click "Generate new token" → fine-grained recommended
  - Choose your repos (or all)
  - Permissions: at minimum, Issues (read+write if you want create_issue),
    Pull requests (read), Contents (read), Metadata (read).
  - For broader cross-repo search, you also need user:email read.

Tools (namespaced as mcp__github__<name>):

  github_list_my_repos(limit?)
  github_get_repo(repo)
  github_search_repos(query, limit?)
  github_list_issues(repo, state?, limit?)
  github_get_issue(repo, issue_number)
  github_list_prs(repo, state?, limit?)
  github_get_pr(repo, pr_number)
  github_create_issue(repo, title, body)         — only call when asked

Read tools are freely callable. The single write tool follows the
personality's "ask before modifying" rule — agent should confirm before
creating an issue on someone's behalf.
"""

from __future__ import annotations

import os
from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

API_BASE = "https://api.github.com"
TIMEOUT_S = 15
ACCEPT = "application/vnd.github+json"
GH_VERSION = "2022-11-28"


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set in .env")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": ACCEPT,
        "X-GitHub-Api-Version": GH_VERSION,
    }


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET a path from api.github.com. Raises on HTTP error."""
    resp = requests.get(f"{API_BASE}{path}", headers=_headers(), params=params, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, json_body: dict[str, Any]) -> Any:
    resp = requests.post(
        f"{API_BASE}{path}",
        headers={**_headers(), "Content-Type": "application/json"},
        json=json_body,
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _format_repo(r: dict[str, Any]) -> str:
    bits = [f"- [{r.get('full_name', '?')}]"]
    if r.get("description"):
        bits.append(r["description"][:120])
    extras = []
    if (s := r.get("stargazers_count")) is not None:
        extras.append(f"⭐{s}")
    if (lang := r.get("language")):
        extras.append(lang)
    if r.get("private"):
        extras.append("private")
    if extras:
        bits.append(f"({', '.join(extras)})")
    return " ".join(bits)


def _format_issue_or_pr(i: dict[str, Any]) -> str:
    return (
        f"- #{i.get('number', '?')} [{i.get('state', '?')}] "
        f"{i.get('title', '')[:100]} "
        f"by @{(i.get('user') or {}).get('login', '?')}"
    )


def create_github_mcp_server() -> McpSdkServerConfig:
    @tool(
        "github_list_my_repos",
        "List repos owned by the authenticated user. Includes private repos the token has access to.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 30."},
            },
            "required": [],
        },
    )
    async def github_list_my_repos(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _get("/user/repos", {
                "per_page": int(args.get("limit", 30)),
                "sort": "updated",
                "affiliation": "owner",
            })
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"github list_my_repos failed: {e}")
        if not data:
            return _ok("(no repos visible to this token)")
        return _ok("\n".join(_format_repo(r) for r in data))

    @tool(
        "github_get_repo",
        "Get details about one repository. `repo` is 'owner/name' format.",
        {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "owner/name"}},
            "required": ["repo"],
        },
    )
    async def github_get_repo(args: dict[str, Any]) -> dict[str, Any]:
        try:
            r = _get(f"/repos/{args['repo']}")
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"github get_repo failed: {e}")
        return _ok(
            f"{r.get('full_name', '?')}\n"
            f"{r.get('description', '') or '(no description)'}\n"
            f"⭐ {r.get('stargazers_count', 0)} | "
            f"forks: {r.get('forks_count', 0)} | "
            f"open issues: {r.get('open_issues_count', 0)} | "
            f"language: {r.get('language', '?')}\n"
            f"default branch: {r.get('default_branch', '?')} | "
            f"private: {r.get('private', False)}\n"
            f"url: {r.get('html_url', '')}"
        )

    @tool(
        "github_search_repos",
        "Search public + accessible repositories by keyword/query.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 10."},
            },
            "required": ["query"],
        },
    )
    async def github_search_repos(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _get("/search/repositories", {
                "q": args["query"],
                "per_page": int(args.get("limit", 10)),
            })
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"github search_repos failed: {e}")
        items = data.get("items", [])
        if not items:
            return _ok("no matching repos.")
        return _ok("\n".join(_format_repo(r) for r in items))

    @tool(
        "github_list_issues",
        (
            "List issues on a repo. `state` is 'open' (default), 'closed', or 'all'. "
            "Note GitHub's issue list also includes PRs by default; use github_list_prs "
            "if you specifically want PRs."
        ),
        {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 20."},
            },
            "required": ["repo"],
        },
    )
    async def github_list_issues(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _get(f"/repos/{args['repo']}/issues", {
                "state": args.get("state", "open"),
                "per_page": int(args.get("limit", 20)),
            })
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"github list_issues failed: {e}")
        # Filter out PRs (which appear in /issues but have a 'pull_request' key).
        issues = [i for i in data if "pull_request" not in i]
        if not issues:
            return _ok("(no matching issues)")
        return _ok("\n".join(_format_issue_or_pr(i) for i in issues))

    @tool(
        "github_get_issue",
        "Get one issue's title, body, state, and up to 20 most-recent comments.",
        {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "issue_number": {"type": "integer"},
            },
            "required": ["repo", "issue_number"],
        },
    )
    async def github_get_issue(args: dict[str, Any]) -> dict[str, Any]:
        repo = args["repo"]
        num = int(args["issue_number"])
        try:
            issue = _get(f"/repos/{repo}/issues/{num}")
            comments = _get(f"/repos/{repo}/issues/{num}/comments", {"per_page": 20}) or []
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"github get_issue failed: {e}")

        body = issue.get("body") or "(no body)"
        if len(body) > 2000:
            body = body[:2000] + "…"
        comment_lines = [
            f"\n@{(c.get('user') or {}).get('login', '?')}: "
            + ((c.get("body") or "")[:400] + ("…" if len(c.get("body") or "") > 400 else ""))
            for c in comments
        ]
        return _ok(
            f"#{issue.get('number')} [{issue.get('state')}] {issue.get('title', '')}\n"
            f"by @{(issue.get('user') or {}).get('login', '?')}\n"
            f"{issue.get('html_url', '')}\n\n"
            f"{body}\n"
            f"\n--- comments ({len(comments)}) ---"
            + ("".join(comment_lines) if comments else "\n(none)")
        )

    @tool(
        "github_list_prs",
        "List pull requests on a repo. `state` is 'open' (default), 'closed', or 'all'.",
        {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["repo"],
        },
    )
    async def github_list_prs(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _get(f"/repos/{args['repo']}/pulls", {
                "state": args.get("state", "open"),
                "per_page": int(args.get("limit", 20)),
            })
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"github list_prs failed: {e}")
        if not data:
            return _ok("(no matching PRs)")
        return _ok("\n".join(_format_issue_or_pr(p) for p in data))

    @tool(
        "github_get_pr",
        (
            "Get one pull request's title, body, state, head/base branches, "
            "review summary, and recent comments. Does NOT include the diff."
        ),
        {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["repo", "pr_number"],
        },
    )
    async def github_get_pr(args: dict[str, Any]) -> dict[str, Any]:
        repo = args["repo"]
        num = int(args["pr_number"])
        try:
            pr = _get(f"/repos/{repo}/pulls/{num}")
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"github get_pr failed: {e}")
        body = pr.get("body") or "(no body)"
        if len(body) > 2000:
            body = body[:2000] + "…"
        head = (pr.get("head") or {}).get("ref", "?")
        base = (pr.get("base") or {}).get("ref", "?")
        return _ok(
            f"#{pr.get('number')} [{pr.get('state')}] {pr.get('title', '')}\n"
            f"by @{(pr.get('user') or {}).get('login', '?')} | "
            f"{head} → {base} | "
            f"+{pr.get('additions', '?')} -{pr.get('deletions', '?')} across "
            f"{pr.get('changed_files', '?')} files\n"
            f"draft: {pr.get('draft', False)} | merged: {pr.get('merged', False)}\n"
            f"{pr.get('html_url', '')}\n\n"
            f"{body}"
        )

    @tool(
        "github_create_issue",
        (
            "Create a new issue on a repo. Per the personality contract, only "
            "call this when the principal explicitly asks to file one. Body "
            "supports Markdown."
        ),
        {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["repo", "title", "body"],
        },
    )
    async def github_create_issue(args: dict[str, Any]) -> dict[str, Any]:
        try:
            issue = _post(
                f"/repos/{args['repo']}/issues",
                {"title": args["title"], "body": args["body"]},
            )
        except (requests.RequestException, RuntimeError) as e:
            return _err(f"github create_issue failed: {e}")
        return _ok(
            f"created #{issue.get('number')}: {issue.get('title')}\n"
            f"{issue.get('html_url', '')}"
        )

    return create_sdk_mcp_server(
        name="github",
        version="1.0.0",
        tools=[
            github_list_my_repos,
            github_get_repo,
            github_search_repos,
            github_list_issues,
            github_get_issue,
            github_list_prs,
            github_get_pr,
            github_create_issue,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "github_server is in-process; instantiate via create_github_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
