"""Gmail MCP server — read, search, draft, archive.

Uses the Gmail API v1 (users.messages, users.drafts) via the shared
google_auth helper.

Tools exposed (namespaced as mcp__gmail__<name>):

  gmail_search(query, max_results?)
      Gmail's search-bar syntax: from:, to:, subject:, after:, before:,
      label:, is:unread, has:attachment, etc.

  gmail_read(email_id)
      Full from/to/subject/date/body/labels for a single message.
      Body is plain-text only and truncated at 10k chars to keep token
      cost reasonable.

  gmail_create_draft(to, subject, body, thread_id?)
      Stages a draft in Gmail Drafts. Returns the draft_id. Tell the
      principal to open Gmail and review/send from the Drafts folder.

  gmail_list_drafts(max_results?)
      List pending drafts so the principal can see what's queued.

  gmail_archive(email_id)        — remove INBOX label
  gmail_mark_read(email_id)      — remove UNREAD label
  gmail_delete_draft(draft_id)   — delete a queued draft

INTENTIONALLY MISSING: any send tool. v1 never auto-sends. Drafts are
staged and the principal sends from Gmail's UI. This is enforced in
three places:
  1. No send tool exists in this file.
  2. The OAuth scope is gmail.modify (no explicit gmail.send scope).
  3. agent_host registers a PreToolUse hook that denies any tool whose
     name contains "send" — so a future contributor can't sneak one in.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from googleapiclient.errors import HttpError

from mcp_servers._untrusted import wrap_untrusted
from mcp_servers.google_auth import build_service


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _gmail():
    return build_service("gmail", "v1")


def _decode_body(payload: dict[str, Any]) -> str:
    """Walk a Gmail message payload and pull out the first text/plain body.

    Falls back to text/html stripped of tags if no plain part exists.
    Returns "" if nothing usable is found.
    """
    def _walk(part: dict[str, Any]) -> str | None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime == "text/plain" and data:
            return base64.urlsafe_b64decode(data).decode(errors="replace")
        for sub in part.get("parts", []) or []:
            found = _walk(sub)
            if found:
                return found
        if mime == "text/html" and data:
            html = base64.urlsafe_b64decode(data).decode(errors="replace")
            # Cheap tag strip — good enough for triage; the agent rarely
            # needs precise HTML structure.
            import re
            return re.sub(r"<[^>]+>", "", html)
        return None

    return _walk(payload) or ""


def _format_message_summary(headers: dict[str, str], snippet: str, msg_id: str) -> str:
    parts = [
        f"[{msg_id}]",
        f"from: {headers.get('From', '')}",
        f"subj: {headers.get('Subject', '')}",
        f"date: {headers.get('Date', '')}",
    ]
    return "\n  ".join(parts) + f"\n  snippet: {snippet[:200]}"


def create_gmail_mcp_server() -> McpSdkServerConfig:
    @tool(
        "gmail_search",
        (
            "Search the principal's Gmail inbox using Gmail's search syntax: "
            "`from:alice`, `subject:invoice`, `is:unread`, `has:attachment`, "
            "`after:2026/01/01`, `label:starred`, etc. Combine with spaces "
            "(AND) or `OR`. Returns id, from, subject, date, snippet for each."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query."},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Max messages to return. Default 10.",
                },
            },
            "required": ["query"],
        },
    )
    async def gmail_search(args: dict[str, Any]) -> dict[str, Any]:
        max_results = int(args.get("max_results", 10))
        try:
            svc = _gmail()
            resp = (
                svc.users()
                .messages()
                .list(userId="me", q=args["query"], maxResults=max_results)
                .execute()
            )
            ids = [m["id"] for m in resp.get("messages", [])]
            if not ids:
                return _ok("no matching messages.")
            entries = []
            for mid in ids:
                msg = (
                    svc.users()
                    .messages()
                    .get(userId="me", id=mid, format="metadata")
                    .execute()
                )
                headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
                entries.append(_format_message_summary(headers, msg.get("snippet", ""), mid))
            # Snippets are sender-controlled — wrap so the agent treats
            # any instruction-shaped text inside them as data, not commands.
            return _ok(wrap_untrusted(
                f"gmail search results for query={args['query']!r}",
                "\n\n".join(entries),
            ))
        except HttpError as e:
            return _err(f"gmail search failed: {e}")

    @tool(
        "gmail_read",
        (
            "Read the full body and headers of one Gmail message by ID. "
            "Use the IDs from gmail_search. Body is plain text, truncated "
            "at 10k chars."
        ),
        {
            "type": "object",
            "properties": {"email_id": {"type": "string"}},
            "required": ["email_id"],
        },
    )
    async def gmail_read(args: dict[str, Any]) -> dict[str, Any]:
        try:
            svc = _gmail()
            msg = (
                svc.users()
                .messages()
                .get(userId="me", id=args["email_id"], format="full")
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            body = _decode_body(msg["payload"])[:10_000]
            labels = msg.get("labelIds", [])
            sender = headers.get("From", "(unknown sender)")
            metadata = (
                f"id: {msg['id']}\n"
                f"thread_id: {msg.get('threadId', '')}\n"
                f"from: {sender}\n"
                f"to: {headers.get('To', '')}\n"
                f"subject: {headers.get('Subject', '')}\n"
                f"date: {headers.get('Date', '')}\n"
                f"labels: {', '.join(labels)}\n"
            )
            # Email body is fully sender-controlled. Wrap so the agent
            # doesn't follow instructions an attacker buried in it.
            return _ok(metadata + "\n" + wrap_untrusted(
                f"gmail email body from {sender}", body
            ))
        except HttpError as e:
            return _err(f"gmail read failed: {e}")

    @tool(
        "gmail_create_draft",
        (
            "Create a draft in Gmail Drafts. The principal will open Gmail "
            "and send it manually — this app never auto-sends. Pass "
            "thread_id to reply within an existing thread. Always show the "
            "full draft to the principal first; this tool only stages it."
        ),
        {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address. Comma-separated for multiple.",
                },
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body."},
                "thread_id": {
                    "type": "string",
                    "description": "Optional Gmail thread ID to reply within an existing conversation.",
                },
            },
            "required": ["to", "subject", "body"],
        },
    )
    async def gmail_create_draft(args: dict[str, Any]) -> dict[str, Any]:
        try:
            mime = MIMEText(args["body"])
            mime["to"] = args["to"]
            mime["subject"] = args["subject"]
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
            body: dict[str, Any] = {"message": {"raw": raw}}
            if args.get("thread_id"):
                body["message"]["threadId"] = args["thread_id"]
            draft = _gmail().users().drafts().create(userId="me", body=body).execute()
            return _ok(
                f"draft created. id: {draft['id']}\n"
                f"open gmail → drafts to review and send. "
                f"this app never auto-sends."
            )
        except HttpError as e:
            return _err(f"gmail create_draft failed: {e}")

    @tool(
        "gmail_list_drafts",
        "List pending Gmail drafts (id + recipient + subject).",
        {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": [],
        },
    )
    async def gmail_list_drafts(args: dict[str, Any]) -> dict[str, Any]:
        try:
            svc = _gmail()
            max_results = int(args.get("max_results", 20))
            resp = svc.users().drafts().list(userId="me", maxResults=max_results).execute()
            drafts = resp.get("drafts", [])
            if not drafts:
                return _ok("no drafts.")
            lines = []
            for d in drafts:
                full = (
                    svc.users()
                    .drafts()
                    .get(userId="me", id=d["id"], format="metadata")
                    .execute()
                )
                headers = {
                    h["name"]: h["value"]
                    for h in full["message"]["payload"].get("headers", [])
                }
                lines.append(
                    f"- [{d['id']}] to: {headers.get('To', '?')} | "
                    f"subj: {headers.get('Subject', '(no subject)')}"
                )
            return _ok("\n".join(lines))
        except HttpError as e:
            return _err(f"gmail list_drafts failed: {e}")

    @tool(
        "gmail_archive",
        "Archive a Gmail message (remove INBOX label). The message stays in All Mail.",
        {
            "type": "object",
            "properties": {"email_id": {"type": "string"}},
            "required": ["email_id"],
        },
    )
    async def gmail_archive(args: dict[str, Any]) -> dict[str, Any]:
        try:
            _gmail().users().messages().modify(
                userId="me", id=args["email_id"], body={"removeLabelIds": ["INBOX"]}
            ).execute()
            return _ok(f"archived [{args['email_id']}]")
        except HttpError as e:
            return _err(f"gmail archive failed: {e}")

    @tool(
        "gmail_mark_read",
        "Mark a Gmail message as read (remove UNREAD label).",
        {
            "type": "object",
            "properties": {"email_id": {"type": "string"}},
            "required": ["email_id"],
        },
    )
    async def gmail_mark_read(args: dict[str, Any]) -> dict[str, Any]:
        try:
            _gmail().users().messages().modify(
                userId="me", id=args["email_id"], body={"removeLabelIds": ["UNREAD"]}
            ).execute()
            return _ok(f"marked read [{args['email_id']}]")
        except HttpError as e:
            return _err(f"gmail mark_read failed: {e}")

    @tool(
        "gmail_delete_draft",
        "Delete a queued Gmail draft by ID.",
        {
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
        },
    )
    async def gmail_delete_draft(args: dict[str, Any]) -> dict[str, Any]:
        try:
            _gmail().users().drafts().delete(userId="me", id=args["draft_id"]).execute()
            return _ok(f"deleted draft [{args['draft_id']}]")
        except HttpError as e:
            return _err(f"gmail delete_draft failed: {e}")

    return create_sdk_mcp_server(
        name="gmail",
        version="1.0.0",
        tools=[
            gmail_search,
            gmail_read,
            gmail_create_draft,
            gmail_list_drafts,
            gmail_archive,
            gmail_mark_read,
            gmail_delete_draft,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "gmail_server is in-process; instantiate via create_gmail_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
