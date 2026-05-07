"""Agent host — the personal agent's reasoning loop.

In step 2 (now) this is a stdin/stdout REPL with no tools. Goal: validate
that the personality lands the way we want before any integrations exist.

In later steps:
  - step 3 wires in the Memory MCP server (audit log + conversation archive)
  - step 4 wires in Gmail / Todoist / Calendar MCP servers
  - step 5 swaps stdin/stdout for the iMessage relay's IPC

The Claude Agent SDK does the heavy lifting here. Two key SDK primitives:
  * `ClaudeAgentOptions` — configuration object for the agent (model,
    system prompt, tool allowlist, etc.). Replaces loose kwargs.
  * `ClaudeSDKClient` — multi-turn session that preserves conversation
    history within a process. Used as an async context manager.

Run it: `python agent_host.py` (after `pip install -e .` in this dir).
Exit with Ctrl-D (EOF) or Ctrl-C.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv

from system_prompt import build_system_prompt

# Load .env before importing the SDK so ANTHROPIC_API_KEY is in place.
load_dotenv()

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

DEFAULT_MODEL = "claude-sonnet-4-6"


def _build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        # The personality + operating rules from config/personality.md, plus
        # current date/time/timezone.
        system_prompt=build_system_prompt(),
        # Pin the model so behavior is reproducible. Override via env var.
        model=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL),
        # Empty allowlist = no tools at all. No file ops, no bash, no web.
        # The agent can only generate text. We add tools in step 3+ via MCP.
        allowed_tools=[],
    )


def _extract_text(message: Any) -> str | None:
    """Pull the assistant's text out of an SDK message object.

    The SDK yields several message kinds — AssistantMessage, UserMessage,
    SystemMessage, ResultMessage. For step 2 we only care about assistant
    text. Each AssistantMessage has a `.content` list of blocks; we want
    the TextBlock instances and ignore the rest (tool use, thinking, etc.).
    """
    if not isinstance(message, AssistantMessage):
        return None
    chunks = [block.text for block in message.content if isinstance(block, TextBlock)]
    return "\n".join(chunks) if chunks else None


async def _read_user_input(prompt: str) -> str | None:
    """Read a line from stdin without blocking the event loop.

    Returns None on EOF (Ctrl-D), an empty string for blank lines, or the
    user's input otherwise.
    """
    loop = asyncio.get_running_loop()
    try:
        # input() is blocking; run it in the default thread-pool executor so
        # the event loop stays responsive.
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

    options = _build_options()

    print(
        "personal agent — step 2 dev REPL\n"
        "(no tools wired in yet; just personality validation)\n"
        "ctrl-d or ctrl-c to exit\n"
    )

    # ClaudeSDKClient is an async context manager — it owns the underlying
    # subprocess/connection that talks to Claude and tears it down cleanly
    # on exit. Conversation history is preserved across turns within the
    # `async with` block.
    async with ClaudeSDKClient(options=options) as client:
        while True:
            user_input = await _read_user_input("you: ")
            if user_input is None:  # EOF
                print("\nbye.")
                return
            user_input = user_input.strip()
            if not user_input:
                continue

            # Send the turn to Claude; iterate the response stream and print
            # any assistant text we encounter.
            await client.query(user_input)

            printed_header = False
            async for message in client.receive_response():
                text = _extract_text(message)
                if text:
                    if not printed_header:
                        print("agent: ", end="")
                        printed_header = True
                    print(text)

            if not printed_header:
                # Agent produced no text this turn — rare but possible.
                print("agent: (no text response)")
            print()  # blank line between turns


def main() -> None:
    try:
        asyncio.run(_chat())
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
