"""Apple Mail.app MCP server (macOS-only, via AppleScript).

For non-Gmail accounts (iCloud, work IMAP, etc.) that aren't covered
by the Gmail sub-agent. **Never sends** — same safety contract as the
Gmail sub-agent. Drafts land in Mail.app's Drafts folder; the principal
hits send manually.

Tools (namespaced as mcp__mail_apple__<name>):
  mail_apple_list_accounts
  mail_apple_search(query, account?, limit?)
  mail_apple_read(message_id, account?)
  mail_apple_draft_reply(message_id, body, account?)
  mail_apple_draft_new(to, subject, body, account?)

Message ids are Mail-internal numeric ids; they're stable within a
session. If you need to operate on a message, fetch a list first to
get the current id.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.applescript import err, escape_str, ok, run_script


def create_mail_apple_mcp_server() -> McpSdkServerConfig:
    @tool(
        "mail_apple_list_accounts",
        "List configured Mail.app accounts. Returns account names + types.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def list_accounts(_args: dict[str, Any]) -> dict[str, Any]:
        script = '''
        tell application "Mail"
            set out to ""
            repeat with a in accounts
                set aname to name of a as string
                set atype to ""
                try
                    set atype to (account type of a as string)
                end try
                set out to out & aname & "|" & atype & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        rows = [line for line in raw.splitlines() if line.strip()]
        if not rows:
            return ok("(no accounts)")
        lines: list[str] = []
        for row in rows:
            parts = row.split("|", 1)
            lines.append(f"- {parts[0]}  ({parts[1] if len(parts) > 1 else '?'})")
        return ok("\n".join(lines))

    @tool(
        "mail_apple_search",
        (
            "Search Mail.app messages by substring across subject + sender. "
            "Optional `account` filter (use mail_apple_list_accounts to find "
            "names). Returns id + sender + subject for each hit."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "account": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    )
    async def search_mail(args: dict[str, Any]) -> dict[str, Any]:
        q = escape_str(args["query"])
        account = args.get("account")
        limit = int(args.get("limit", 20))

        # Searching across all inboxes is reasonable for v1. Mail's
        # "whose" filter on messages is slow for huge mailboxes —
        # cap the search to the inbox messages set.
        if account:
            scope = (
                f'(messages of inbox of account "{escape_str(account)}" '
                f'whose subject contains "{q}" or sender contains "{q}")'
            )
        else:
            scope = (
                f'(messages of inbox whose subject contains "{q}" or '
                f'sender contains "{q}")'
            )
        script = f'''
        tell application "Mail"
            set out to ""
            set hits to {scope}
            set n to 0
            repeat with m in hits
                if n ≥ {limit} then exit repeat
                set n to n + 1
                set mid_ to id of m
                set msender to sender of m as string
                set msubj to subject of m as string
                set out to out & mid_ & "|" & msender & "|" & msubj & linefeed
            end repeat
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        rows = [line for line in raw.splitlines() if line.strip()]
        if not rows:
            return ok("(no matching mail)")
        lines: list[str] = []
        for row in rows:
            parts = row.split("|", 2)
            mid = parts[0] if parts else "?"
            sender = parts[1] if len(parts) > 1 else "?"
            subj = parts[2] if len(parts) > 2 else "(no subject)"
            lines.append(f"- [{mid}] from {sender}: {subj}")
        return ok("\n".join(lines))

    @tool(
        "mail_apple_read",
        "Read a Mail message body by id. Returns headers + plaintext content.",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "account": {"type": "string"},
            },
            "required": ["message_id"],
        },
    )
    async def read_mail(args: dict[str, Any]) -> dict[str, Any]:
        mid = escape_str(args["message_id"])
        # `id` is an integer-typed AppleScript property; comparison needs
        # the unquoted form. We coerce both sides to strings via `as string`
        # in the filter for safety.
        script = f'''
        tell application "Mail"
            set hits to (every message of every mailbox whose (id as string) is "{mid}")
            if (count of hits) is 0 then
                return "not found"
            end if
            set m to item 1 of hits
            set out to "From: " & (sender of m) & linefeed
            set out to out & "Subject: " & (subject of m) & linefeed
            set out to out & "Date: " & (date received of m) & linefeed
            set out to out & "---" & linefeed
            set out to out & (content of m as string)
            return out
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        if raw.strip() == "not found":
            return err(f"message id {args['message_id']} not found")
        return ok(raw)

    @tool(
        "mail_apple_draft_reply",
        (
            "Create a reply DRAFT to an existing message. Lands in the "
            "Drafts folder — NEVER sent. The principal sends manually. "
            "Match the Gmail safety contract."
        ),
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["message_id", "body"],
        },
    )
    async def draft_reply(args: dict[str, Any]) -> dict[str, Any]:
        mid = escape_str(args["message_id"])
        body = escape_str(args["body"])
        script = f'''
        tell application "Mail"
            set hits to (every message of every mailbox whose (id as string) is "{mid}")
            if (count of hits) is 0 then
                return "not found"
            end if
            set m to item 1 of hits
            set replyMsg to reply m with opening window
            set content of replyMsg to "{body}"
            save replyMsg
            return "drafted: " & subject of replyMsg
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        if raw.strip() == "not found":
            return err(f"message id {args['message_id']} not found")
        return ok(raw)

    @tool(
        "mail_apple_draft_new",
        "Create a new outgoing DRAFT (never sends). Lands in Drafts.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "account": {
                    "type": "string",
                    "description": "Sender account name (default: first configured).",
                },
            },
            "required": ["to", "subject", "body"],
        },
    )
    async def draft_new(args: dict[str, Any]) -> dict[str, Any]:
        to = escape_str(args["to"])
        subject = escape_str(args["subject"])
        body = escape_str(args["body"])
        account = args.get("account")
        sender_clause = ""
        if account:
            sender_clause = (
                f',\n                sender:(get email address of account "{escape_str(account)}")'
            )
        script = f'''
        tell application "Mail"
            set newMsg to make new outgoing message with properties {{subject:"{subject}", content:"{body}", visible:true{sender_clause}}}
            tell newMsg
                make new to recipient at end of to recipients with properties {{address:"{to}"}}
            end tell
            save newMsg
            return "drafted: " & subject of newMsg
        end tell
        '''
        try:
            raw = run_script(script)
        except RuntimeError as e:
            return err(str(e))
        return ok(raw)

    return create_sdk_mcp_server(
        name="mail_apple",
        version="1.0.0",
        tools=[list_accounts, search_mail, read_mail, draft_reply, draft_new],
    )


def main() -> None:
    raise NotImplementedError(
        "mail_apple_server is in-process; instantiate via create_mail_apple_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
