"""Gmail MCP server — read, search, draft, archive.

Exposes:
  - gmail_search(query, max_results)        — search inbox via Gmail query syntax
  - gmail_read(message_id)                  — full body + headers
  - gmail_create_draft(to, subject, body, thread_id?) — stages to Gmail Drafts
  - gmail_list_drafts()                     — list pending drafts
  - gmail_archive(message_id)               — remove INBOX label
  - gmail_mark_read(message_id)             — remove UNREAD label

INTENTIONALLY MISSING: any "send" tool. v1 never auto-sends. Drafts are
staged and the user sends manually from Gmail's UI. This is enforced
by the SDK pre-tool hook in agent_host.py — even if a future contributor
adds a send-shaped tool to this file, the hook blocks the call.

Built in step 4 of the v1 plan.
"""


def main() -> None:
    raise NotImplementedError("Gmail MCP server not yet implemented — see step 4 of the v1 plan.")


if __name__ == "__main__":
    main()
