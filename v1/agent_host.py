"""Agent host — the personal agent's reasoning loop.

Step 3 (now): adds the Memory MCP server and persistent storage. Every
user turn, every assistant message, and every tool call is logged to
`data/memory.sqlite` — both as conversation archive (for "what did we
talk about") and as audit log (privacy invariant: nothing flows to
Anthropic that the principal can't see locally).

The agent can call three memory tools:
  - mcp__memory__memory_log_fact (capture durable facts)
  - mcp__memory__memory_recall_facts (look up stored facts)
  - mcp__memory__memory_search_conversations (substring search history)

In later steps:
  - step 4 wires Gmail / Todoist / Calendar MCP servers
  - step 5 swaps stdin/stdout for the iMessage relay's IPC

Two key Claude Agent SDK primitives at play:
  * `ClaudeAgentOptions` — config object (model, system prompt, tool
    allowlist, MCP servers).
  * `ClaudeSDKClient` — multi-turn session that preserves conversation
    history within a process. Used as an async context manager.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv

# Load .env before importing the SDK so ANTHROPIC_API_KEY is in place.
load_dotenv()

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PreToolUseHookInput,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import HookContext  # noqa: E402

from memory.store import MemoryStore  # noqa: E402
from mcp_servers.calendar_server import create_calendar_mcp_server  # noqa: E402
from mcp_servers.gmail_server import create_gmail_mcp_server  # noqa: E402
from mcp_servers.memory_server import create_memory_mcp_server  # noqa: E402
from mcp_servers.todoist_server import create_todoist_mcp_server  # noqa: E402
from system_prompt import build_system_prompt  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-4-6"

# The MCP tools the agent is permitted to call. Each name follows the SDK
# convention `mcp__<server-name>__<tool-name>`. Anything not on this list
# is blocked even if the MCP server exposes it.
MEMORY_TOOLS = [
    "mcp__memory__memory_log_fact",
    "mcp__memory__memory_recall_facts",
    "mcp__memory__memory_search_conversations",
]

TODOIST_TOOLS = [
    "mcp__todoist__todoist_list_tasks",
    "mcp__todoist__todoist_create_task",
    "mcp__todoist__todoist_update_task",
    "mcp__todoist__todoist_complete_task",
    "mcp__todoist__todoist_list_projects",
    "mcp__todoist__todoist_list_labels",
]

GMAIL_TOOLS = [
    "mcp__gmail__gmail_search",
    "mcp__gmail__gmail_read",
    "mcp__gmail__gmail_create_draft",
    "mcp__gmail__gmail_list_drafts",
    "mcp__gmail__gmail_archive",
    "mcp__gmail__gmail_mark_read",
    "mcp__gmail__gmail_delete_draft",
]

CALENDAR_TOOLS = [
    "mcp__calendar__calendar_list_events",
    "mcp__calendar__calendar_search_events",
    "mcp__calendar__calendar_check_availability",
    "mcp__calendar__calendar_get_event",
]


# ─── Safety hooks ───────────────────────────────────────────────────────────
#
# Belt-and-suspenders: even though we never expose a send-shaped tool, this
# PreToolUse hook denies any tool call whose name contains "send" (case-
# insensitive). Triple defense alongside the system prompt and the absent
# tool surface. Returns the SDK's "deny" decision shape:
#   { "hookSpecificOutput": { ... permissionDecision: "deny" ... } }


async def _block_send_tools(
    input_data: PreToolUseHookInput,
    _tool_use_id: str | None,
    _context: HookContext,
) -> dict[str, Any]:
    name = input_data.get("tool_name", "") if isinstance(input_data, dict) else getattr(input_data, "tool_name", "")
    if "send" in (name or "").lower():
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"tool '{name}' blocked by no-auto-send invariant. "
                    "drafts must go to Gmail Drafts; the principal sends manually."
                ),
            }
        }
    return {}


def _build_options(store: MemoryStore) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        # Personality + runtime context + injected facts.
        system_prompt=build_system_prompt(store),
        # Pin the model so behavior is reproducible. Override via env var.
        model=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL),
        # In-process MCP servers — no subprocess overhead. The memory
        # factory closes over `store` so its tools share the same SQLite
        # handle the agent_host uses for archive/audit writes.
        mcp_servers={
            "memory": create_memory_mcp_server(store),
            "todoist": create_todoist_mcp_server(),
            "gmail": create_gmail_mcp_server(),
            "calendar": create_calendar_mcp_server(),
        },
        # Allowlist what tools the agent may call. Anything not listed here
        # is blocked. v1 integrations are now complete.
        allowed_tools=MEMORY_TOOLS + TODOIST_TOOLS + GMAIL_TOOLS + CALENDAR_TOOLS,
        # Isolate the agent from the user's Claude Code environment:
        #   * `tools=[]` disables built-in CLI primitives (Bash, Read, Edit,
        #     ToolSearch, etc.). The agent runs ONLY our MCP-defined tools.
        #   * `strict_mcp_config=True` ignores user/project/plugin MCP server
        #     configs (notably the user's claude.ai integrations like
        #     mcp__claude_ai_Google_Calendar__*) so the agent doesn't shop
        #     for fallbacks when one of our local tools errors.
        tools=[],
        strict_mcp_config=True,
        # Safety hook: deny anything with "send" in the tool name. The
        # matcher=".*" runs the hook on every tool call regardless of name.
        hooks={
            "PreToolUse": [HookMatcher(matcher=".*", hooks=[_block_send_tools])],
        },
    )


def _extract_text(message: Any) -> str | None:
    """Pull the assistant's text out of an AssistantMessage."""
    if not isinstance(message, AssistantMessage):
        return None
    chunks = [block.text for block in message.content if isinstance(block, TextBlock)]
    return "\n".join(chunks) if chunks else None


def _extract_tool_calls(message: Any) -> list[dict[str, Any]]:
    """Pull tool-use blocks out of an AssistantMessage for archiving."""
    if not isinstance(message, AssistantMessage):
        return []
    return [
        {"name": b.name, "input": b.input, "id": b.id}
        for b in message.content
        if isinstance(b, ToolUseBlock)
    ]


async def _read_user_input(prompt: str) -> str | None:
    """Read a line from stdin without blocking the event loop."""
    loop = asyncio.get_running_loop()
    try:
        line = await loop.run_in_executor(None, input, prompt)
    except EOFError:
        return None
    return line


async def _chat() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "error: ANTHROPIC_API_KEY is not set. copy .env.example to .env "
            "and fill it in.",
            file=sys.stderr,
        )
        sys.exit(1)

    store = MemoryStore()
    conversation_id = store.open_conversation(source="cli")
    options = _build_options(store)

    print(
        "personal agent — step 3 dev REPL\n"
        f"(memory wired in; conversation_id={conversation_id})\n"
        "ctrl-d or ctrl-c to exit\n"
    )

    try:
        async with ClaudeSDKClient(options=options) as client:
            while True:
                user_input = await _read_user_input("you: ")
                if user_input is None:  # EOF
                    print("\nbye.")
                    return
                user_input = user_input.strip()
                if not user_input:
                    continue

                # Archive + audit the user's turn before sending it.
                store.append_message(conversation_id, "user", user_input)
                store.log_api_event("user_input", user_input, conversation_id)

                await client.query(user_input)

                printed_header = False
                async for message in client.receive_response():
                    text = _extract_text(message)
                    tool_calls = _extract_tool_calls(message)

                    if text or tool_calls:
                        # One row per assistant message in the archive,
                        # capturing both text and any tool calls it requested.
                        store.append_message(
                            conversation_id,
                            "assistant",
                            text or "",
                            tool_calls=tool_calls or None,
                        )
                        if text:
                            store.log_api_event("assistant_text", text, conversation_id)
                        for tc in tool_calls:
                            store.log_api_event("tool_use", tc, conversation_id)

                    # ResultMessage carries usage/cost info — log it.
                    if isinstance(message, ResultMessage):
                        meta = {}
                        for attr in ("total_cost_usd", "duration_ms", "num_turns"):
                            v = getattr(message, attr, None)
                            if v is not None:
                                meta[attr] = v
                        usage = getattr(message, "usage", None)
                        if usage is not None:
                            meta["usage"] = (
                                usage.__dict__ if hasattr(usage, "__dict__") else str(usage)
                            )
                        store.log_api_event(
                            "result", str(message), conversation_id, metadata=meta
                        )

                    if text:
                        if not printed_header:
                            print("agent: ", end="")
                            printed_header = True
                        print(text)

                if not printed_header:
                    print("agent: (no text response)")
                print()
    finally:
        store.close_conversation(conversation_id)


def main() -> None:
    try:
        asyncio.run(_chat())
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
