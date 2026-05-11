"""LinkedIn MCP server — profile + post creation.

LinkedIn's API for personal apps is narrow: OIDC userinfo for profile,
and `w_member_social` for creating posts. That's the entire useful
surface — analytics, search, connection management, and most other
endpoints are gated behind partner approval (Marketing / Talent /
Recruiter products).

Tools exposed (namespaced as mcp__linkedin__<name>):

  linkedin_get_profile()
      Pull the principal's OIDC userinfo: name, email, picture, URN.
      Useful for "what's my LinkedIn URN" / sanity-checking auth.

  linkedin_post_share(text, visibility?)
      Create a text post on the principal's LinkedIn feed. visibility
      is 'PUBLIC' (default) or 'CONNECTIONS'. Returns the post ID and
      a link to view it.

Per the personality contract, never create a post without the principal's
explicit instruction.
"""

from __future__ import annotations

from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.linkedin_auth import get_access_token, get_user_urn

API_BASE = "https://api.linkedin.com"
USERINFO_URL = f"{API_BASE}/v2/userinfo"
UGC_POSTS_URL = f"{API_BASE}/v2/ugcPosts"
TIMEOUT_S = 15


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    out = {"Authorization": f"Bearer {get_access_token()}"}
    if extra:
        out.update(extra)
    return out


def _explain_error(resp: requests.Response) -> str:
    if resp.status_code == 401:
        return (
            "linkedin auth expired or invalid — re-run "
            "`python -m mcp_servers.linkedin_auth` to refresh."
        )
    if resp.status_code == 403:
        return "linkedin forbidden — the requested action isn't covered by the granted scopes."
    if resp.status_code == 429:
        return "linkedin rate-limited. wait a few seconds and retry."
    return f"linkedin HTTP {resp.status_code}: {resp.text[:300]}"


def create_linkedin_mcp_server() -> McpSdkServerConfig:
    @tool(
        "linkedin_get_profile",
        (
            "Return the principal's LinkedIn profile via OIDC userinfo: "
            "name, email, picture URL, and the URN used internally for "
            "post creation. Read-only."
        ),
        {"type": "object", "properties": {}, "required": []},
    )
    async def linkedin_get_profile(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.get(USERINFO_URL, headers=_headers(), timeout=TIMEOUT_S)
        except requests.RequestException as e:
            return _err(f"linkedin get_profile failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))
        data = resp.json() or {}
        return _ok(
            f"name: {data.get('name', '?')}\n"
            f"email: {data.get('email', '?')}\n"
            f"sub (urn id): {data.get('sub', '?')}\n"
            f"picture: {data.get('picture', '')}\n"
            f"locale: {data.get('locale', '?')}"
        )

    @tool(
        "linkedin_post_share",
        (
            "Create a text post on the principal's LinkedIn feed. "
            "`text` is the post body (max ~3000 chars per LinkedIn). "
            "`visibility` is 'PUBLIC' (default) or 'CONNECTIONS'. "
            "Per the personality contract, NEVER call this without the "
            "principal explicitly asking to post — confirm the exact "
            "text and visibility before posting."
        ),
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "visibility": {
                    "type": "string",
                    "enum": ["PUBLIC", "CONNECTIONS"],
                    "description": "Default PUBLIC.",
                },
            },
            "required": ["text"],
        },
    )
    async def linkedin_post_share(args: dict[str, Any]) -> dict[str, Any]:
        try:
            author_urn = get_user_urn()
        except RuntimeError as e:
            return _err(str(e))

        visibility = args.get("visibility", "PUBLIC").upper()
        body = {
            "author": author_urn,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": args["text"]},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": visibility,
            },
        }
        try:
            resp = requests.post(
                UGC_POSTS_URL,
                headers=_headers({
                    "Content-Type": "application/json",
                    "X-Restli-Protocol-Version": "2.0.0",
                }),
                json=body,
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as e:
            return _err(f"linkedin post_share failed: {e}")
        if not resp.ok:
            return _err(_explain_error(resp))

        # LinkedIn returns the post URN in the X-RestLi-Id header on success.
        post_urn = resp.headers.get("X-RestLi-Id") or "?"
        # Construct a human URL — LinkedIn's URN format for shares is
        # urn:li:share:<id> which maps to linkedin.com/feed/update/urn:li:share:<id>
        view_url = ""
        if post_urn and post_urn.startswith("urn:li:"):
            view_url = f"https://www.linkedin.com/feed/update/{post_urn}/"
        return _ok(
            f"posted to {visibility}.\n"
            f"urn: {post_urn}\n"
            f"link: {view_url}"
        )

    return create_sdk_mcp_server(
        name="linkedin",
        version="1.0.0",
        tools=[linkedin_get_profile, linkedin_post_share],
    )


def main() -> None:
    raise NotImplementedError(
        "linkedin_server is in-process; instantiate via create_linkedin_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
