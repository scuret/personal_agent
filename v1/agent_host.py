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
from pathlib import Path
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
from mcp_servers.archive_server import create_archive_mcp_server  # noqa: E402
from mcp_servers.calendar_server import create_calendar_mcp_server  # noqa: E402
from mcp_servers.canva_server import create_canva_mcp_server  # noqa: E402
from mcp_servers.docs_server import create_docs_mcp_server  # noqa: E402
from mcp_servers.drive_server import create_drive_mcp_server  # noqa: E402
from mcp_servers.dropbox_server import create_dropbox_mcp_server  # noqa: E402
from mcp_servers.eightsleep_server import create_eightsleep_mcp_server  # noqa: E402
from mcp_servers.github_server import create_github_mcp_server  # noqa: E402
from mcp_servers.gmail_server import create_gmail_mcp_server  # noqa: E402
from mcp_servers.linkedin_server import create_linkedin_mcp_server  # noqa: E402
from mcp_servers.mail_apple_server import create_mail_apple_mcp_server  # noqa: E402
from mcp_servers.maps_server import create_maps_mcp_server  # noqa: E402
from mcp_servers.memory_server import create_memory_mcp_server  # noqa: E402
from mcp_servers.music_apple_server import create_music_apple_mcp_server  # noqa: E402
from mcp_servers.notes_apple_server import create_notes_apple_mcp_server  # noqa: E402
from mcp_servers.notion_server import create_notion_mcp_server  # noqa: E402
from mcp_servers.photos_apple_server import create_photos_apple_mcp_server  # noqa: E402
from mcp_servers.reddit_server import create_reddit_mcp_server  # noqa: E402
from mcp_servers.reminders_apple_server import create_reminders_apple_mcp_server  # noqa: E402
from mcp_servers.reminders_server import create_reminders_mcp_server  # noqa: E402
from mcp_servers.sheets_server import create_sheets_mcp_server  # noqa: E402
from mcp_servers.spotify_server import create_spotify_mcp_server  # noqa: E402
from mcp_servers.todoist_server import create_todoist_mcp_server  # noqa: E402
from mcp_servers.vision_server import create_vision_mcp_server  # noqa: E402
from mcp_servers.weather_server import create_weather_mcp_server  # noqa: E402
from mcp_servers.web_server import create_web_mcp_server  # noqa: E402
from mcp_servers.wikipedia_server import create_wikipedia_mcp_server  # noqa: E402
from mcp_servers.youtube_server import create_youtube_mcp_server  # noqa: E402
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
    "mcp__calendar__calendar_create_event",
    "mcp__calendar__calendar_update_event",
    "mcp__calendar__calendar_delete_event",
]

DRIVE_TOOLS = [
    "mcp__drive__drive_search",
    "mcp__drive__drive_list_folder",
    "mcp__drive__drive_get_metadata",
    "mcp__drive__drive_read_text",
    "mcp__drive__drive_create_share_link",
]

DOCS_TOOLS = [
    "mcp__docs__docs_read",
    "mcp__docs__docs_append_text",
    "mcp__docs__docs_replace_text",
    "mcp__docs__docs_create",
]

SHEETS_TOOLS = [
    "mcp__sheets__sheets_read_range",
    "mcp__sheets__sheets_append_rows",
    "mcp__sheets__sheets_update_range",
    "mcp__sheets__sheets_create",
]

SPOTIFY_TOOLS = [
    "mcp__spotify__spotify_search",
    "mcp__spotify__spotify_currently_playing",
    "mcp__spotify__spotify_play",
    "mcp__spotify__spotify_pause",
    "mcp__spotify__spotify_queue",
    "mcp__spotify__spotify_list_playlists",
    "mcp__spotify__spotify_list_devices",
]

CANVA_TOOLS = [
    "mcp__canva__canva_list_designs",
    "mcp__canva__canva_get_design",
    "mcp__canva__canva_create_design",
    "mcp__canva__canva_export_design",
    "mcp__canva__canva_list_folders",
]

LINKEDIN_TOOLS = [
    "mcp__linkedin__linkedin_get_profile",
    "mcp__linkedin__linkedin_post_share",
]

# Apple-native sub-agents — macOS-only, no auth (AppleScript bridge).
REMINDERS_APPLE_TOOLS = [
    "mcp__reminders_apple__reminders_apple_list_lists",
    "mcp__reminders_apple__reminders_apple_list",
    "mcp__reminders_apple__reminders_apple_create",
    "mcp__reminders_apple__reminders_apple_complete",
    "mcp__reminders_apple__reminders_apple_delete",
]
NOTES_APPLE_TOOLS = [
    "mcp__notes_apple__notes_apple_list",
    "mcp__notes_apple__notes_apple_search",
    "mcp__notes_apple__notes_apple_read",
    "mcp__notes_apple__notes_apple_append",
    "mcp__notes_apple__notes_apple_create",
]
PHOTOS_APPLE_TOOLS = [
    "mcp__photos_apple__photos_apple_list_albums",
    "mcp__photos_apple__photos_apple_recent",
    "mcp__photos_apple__photos_apple_search_by_date",
    "mcp__photos_apple__photos_apple_get_album",
]
MUSIC_APPLE_TOOLS = [
    "mcp__music_apple__music_apple_now_playing",
    "mcp__music_apple__music_apple_play",
    "mcp__music_apple__music_apple_pause",
    "mcp__music_apple__music_apple_next",
    "mcp__music_apple__music_apple_previous",
    "mcp__music_apple__music_apple_search_and_play",
    "mcp__music_apple__music_apple_list_playlists",
]
MAIL_APPLE_TOOLS = [
    "mcp__mail_apple__mail_apple_list_accounts",
    "mcp__mail_apple__mail_apple_search",
    "mcp__mail_apple__mail_apple_read",
    "mcp__mail_apple__mail_apple_draft_reply",
    "mcp__mail_apple__mail_apple_draft_new",
]

MAPS_TOOLS = [
    "mcp__maps__maps_search_places",
    "mcp__maps__maps_drive_time",
    "mcp__maps__maps_geocode",
    "mcp__maps__maps_reverse_geocode",
]

EIGHTSLEEP_TOOLS = [
    "mcp__eightsleep__eightsleep_last_night",
    "mcp__eightsleep__eightsleep_current_state",
    "mcp__eightsleep__eightsleep_set_temp",
]

WEATHER_TOOLS = [
    "mcp__weather__weather_current",
    "mcp__weather__weather_forecast",
]

VISION_TOOLS = [
    "mcp__vision__analyze_image",
]

NOTION_TOOLS = [
    "mcp__notion__notion_search",
    "mcp__notion__notion_get_page",
    "mcp__notion__notion_query_database",
    "mcp__notion__notion_create_page",
    "mcp__notion__notion_append_text",
]

GITHUB_TOOLS = [
    "mcp__github__github_list_my_repos",
    "mcp__github__github_get_repo",
    "mcp__github__github_search_repos",
    "mcp__github__github_list_issues",
    "mcp__github__github_get_issue",
    "mcp__github__github_list_prs",
    "mcp__github__github_get_pr",
    "mcp__github__github_create_issue",
]

WEB_TOOLS = [
    "mcp__web__web_search",
    "mcp__web__web_fetch",
]

REMINDER_TOOLS = [
    "mcp__reminders__remind",
    "mcp__reminders__remind_recurring",
    "mcp__reminders__list_reminders",
    "mcp__reminders__cancel_reminder",
]

YOUTUBE_TOOLS = [
    "mcp__youtube__youtube_search",
    "mcp__youtube__youtube_get_video",
    "mcp__youtube__youtube_get_channel",
    "mcp__youtube__youtube_list_channel_uploads",
]

DROPBOX_TOOLS = [
    "mcp__dropbox__dropbox_search",
    "mcp__dropbox__dropbox_list_folder",
    "mcp__dropbox__dropbox_get_metadata",
    "mcp__dropbox__dropbox_download_text",
    "mcp__dropbox__dropbox_create_share_link",
]

WIKIPEDIA_TOOLS = [
    "mcp__wikipedia__wiki_search",
    "mcp__wikipedia__wiki_summary",
    "mcp__wikipedia__wiki_get_article",
]

REDDIT_TOOLS = [
    "mcp__reddit__reddit_subreddit_top",
    "mcp__reddit__reddit_subreddit_hot",
    "mcp__reddit__reddit_search",
    "mcp__reddit__reddit_get_post",
]

ARCHIVE_TOOLS = [
    "mcp__archive__archive_activity_summary",
    "mcp__archive__archive_top_tools",
    "mcp__archive__archive_recent_conversations",
    "mcp__archive__archive_activity_by_hour",
]


# ─── Sub-agent enablement ───────────────────────────────────────────────────
#
# Each sub-agent is registered only when its required configuration is
# present. Lets the install script control capability surface: if the
# user didn't configure a Notion token, Notion isn't registered, the
# agent doesn't see those tools at all, no wasted system-prompt tokens,
# no "tool failed" surprises at call-time.
#
# Always-on agents (memory, weather, vision, wikipedia, reddit, reminders)
# need no auth beyond ANTHROPIC_API_KEY (which is required for the whole
# app), so they're always registered.


def _has_env(*names: str) -> bool:
    """True iff every named env var is set + non-empty."""
    return all(bool((os.environ.get(n) or "").strip()) for n in names)


def _has_google_oauth() -> bool:
    """True iff a Google OAuth client JSON is present.

    We don't require the cached token pickle here — if it's missing,
    the first tool call triggers the OAuth flow (which works in dev/CLI
    but not under launchd; install.sh handles the dance up-front).
    """
    creds_raw = os.environ.get("GOOGLE_OAUTH_CREDENTIALS_PATH", "./config/credentials.json")
    creds_path = Path(creds_raw)
    if not creds_path.is_absolute():
        creds_path = Path(__file__).parent / creds_path
    return creds_path.exists()


def _has_dropbox_oauth() -> bool:
    """True iff Dropbox app creds AND a cached refresh token are present.

    Both pieces are required: the app key/secret to talk to the OAuth
    endpoints, plus the cached refresh_token from the one-time consent
    flow. If the cache is missing, the sub-agent stays unregistered so
    the agent doesn't try to call tools that will fail.
    """
    if not _has_env("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"):
        return False
    token_raw = os.environ.get("DROPBOX_TOKEN_PATH", "./data/dropbox_token.json")
    token_path = Path(token_raw)
    if not token_path.is_absolute():
        token_path = Path(__file__).parent / token_path
    return token_path.exists()


def _has_eightsleep() -> bool:
    """True iff Eight Sleep email+password are present. The auth module
    re-logs in as needed; no cached token gate (a stale cache just
    triggers a re-login on first tool call)."""
    return _has_env("EIGHT_EMAIL", "EIGHT_PASSWORD")


def _maps_available() -> bool:
    """Maps sub-agent is always available — falls back to OSM (no auth)
    when GOOGLE_MAPS_API_KEY isn't set. The provider abstraction in
    mcp_servers/maps_providers/__init__.py picks at startup time."""
    return True


def _is_macos() -> bool:
    """Platform gate for Apple-native sub-agents — only register them when
    the daemon is running on macOS. AppleScript via py-applescript isn't
    available on Linux/Windows; gating here keeps the agent's tool surface
    clean on those platforms (vs registering tools that always fail)."""
    import sys
    return sys.platform == "darwin"


def _has_spotify_oauth() -> bool:
    """True iff Spotify app creds AND a cached refresh token are present."""
    if not _has_env("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"):
        return False
    token_raw = os.environ.get("SPOTIFY_TOKEN_PATH", "./data/spotify_token.json")
    token_path = Path(token_raw)
    if not token_path.is_absolute():
        token_path = Path(__file__).parent / token_path
    return token_path.exists()


def _has_canva_oauth() -> bool:
    """True iff Canva app creds AND a cached refresh token are present."""
    if not _has_env("CANVA_CLIENT_ID", "CANVA_CLIENT_SECRET"):
        return False
    token_raw = os.environ.get("CANVA_TOKEN_PATH", "./data/canva_token.json")
    token_path = Path(token_raw)
    if not token_path.is_absolute():
        token_path = Path(__file__).parent / token_path
    return token_path.exists()


def _has_linkedin_oauth() -> bool:
    """True iff LinkedIn app creds AND a cached access token are present.

    LinkedIn personal-tier doesn't issue refresh tokens, so we only
    check for the access token file. The auth helper enforces expiry
    at use time.
    """
    if not _has_env("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"):
        return False
    token_raw = os.environ.get("LINKEDIN_TOKEN_PATH", "./data/linkedin_token.json")
    token_path = Path(token_raw)
    if not token_path.is_absolute():
        token_path = Path(__file__).parent / token_path
    return token_path.exists()


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


def build_options(store: MemoryStore, model: str | None = None) -> ClaudeAgentOptions:
    # Sub-agent registration table. Each entry: (name, factory, tool list,
    # is_enabled callable). Filtered by enablement — only configured
    # integrations register, so unused tools don't bloat the system prompt
    # and the agent never tries to call something that'd fail.
    candidates: list[tuple[str, Any, list[str], Any]] = [
        ("memory",    lambda: create_memory_mcp_server(store),     MEMORY_TOOLS,    lambda: True),
        ("archive",   lambda: create_archive_mcp_server(store),    ARCHIVE_TOOLS,   lambda: True),
        ("weather",   lambda: create_weather_mcp_server(),         WEATHER_TOOLS,   lambda: True),
        ("vision",    lambda: create_vision_mcp_server(store),     VISION_TOOLS,    lambda: True),
        ("wikipedia", lambda: create_wikipedia_mcp_server(),       WIKIPEDIA_TOOLS, lambda: True),
        ("reddit",    lambda: create_reddit_mcp_server(),          REDDIT_TOOLS,    lambda: True),
        ("reminders", lambda: create_reminders_mcp_server(store),  REMINDER_TOOLS,  lambda: True),
        ("todoist",   lambda: create_todoist_mcp_server(),         TODOIST_TOOLS,   lambda: _has_env("TODOIST_API_KEY")),
        ("gmail",     lambda: create_gmail_mcp_server(),           GMAIL_TOOLS,     _has_google_oauth),
        ("calendar",  lambda: create_calendar_mcp_server(),        CALENDAR_TOOLS,  _has_google_oauth),
        ("drive",     lambda: create_drive_mcp_server(),           DRIVE_TOOLS,     _has_google_oauth),
        ("docs",      lambda: create_docs_mcp_server(),            DOCS_TOOLS,      _has_google_oauth),
        ("sheets",    lambda: create_sheets_mcp_server(),          SHEETS_TOOLS,    _has_google_oauth),
        ("notion",    lambda: create_notion_mcp_server(),          NOTION_TOOLS,    lambda: _has_env("NOTION_INTEGRATION_TOKEN")),
        ("github",    lambda: create_github_mcp_server(),          GITHUB_TOOLS,    lambda: _has_env("GITHUB_TOKEN")),
        ("web",       lambda: create_web_mcp_server(),             WEB_TOOLS,       lambda: _has_env("BRAVE_SEARCH_API_KEY")),
        ("youtube",   lambda: create_youtube_mcp_server(),         YOUTUBE_TOOLS,   lambda: _has_env("YOUTUBE_API_KEY")),
        ("dropbox",   lambda: create_dropbox_mcp_server(),         DROPBOX_TOOLS,   _has_dropbox_oauth),
        ("spotify",   lambda: create_spotify_mcp_server(),         SPOTIFY_TOOLS,   _has_spotify_oauth),
        ("canva",     lambda: create_canva_mcp_server(),           CANVA_TOOLS,     _has_canva_oauth),
        ("linkedin",  lambda: create_linkedin_mcp_server(),        LINKEDIN_TOOLS,  _has_linkedin_oauth),
        # Apple-native (AppleScript bridge). All gated on macOS — Linux/Windows
        # forkers won't see these registered. No auth beyond the daemon
        # running with permission to drive these apps (Automation prompt
        # on first use).
        ("reminders_apple", lambda: create_reminders_apple_mcp_server(), REMINDERS_APPLE_TOOLS, _is_macos),
        ("notes_apple",     lambda: create_notes_apple_mcp_server(),     NOTES_APPLE_TOOLS,     _is_macos),
        ("photos_apple",    lambda: create_photos_apple_mcp_server(),    PHOTOS_APPLE_TOOLS,    _is_macos),
        ("music_apple",     lambda: create_music_apple_mcp_server(),     MUSIC_APPLE_TOOLS,     _is_macos),
        ("mail_apple",      lambda: create_mail_apple_mcp_server(),      MAIL_APPLE_TOOLS,      _is_macos),
        # Maps — always-on; OSM fallback covers the no-key case so fork-
        # and-run users get this for free.
        ("maps",            lambda: create_maps_mcp_server(),            MAPS_TOOLS,            _maps_available),
        # Eight Sleep — unofficial API, gated on email+password presence.
        ("eightsleep",      lambda: create_eightsleep_mcp_server(),      EIGHTSLEEP_TOOLS,      _has_eightsleep),
    ]

    mcp_servers: dict[str, Any] = {}
    allowed_tools: list[str] = []
    enabled_names: list[str] = []
    for name, factory, tools, is_enabled in candidates:
        if is_enabled():
            mcp_servers[name] = factory()
            allowed_tools.extend(tools)
            enabled_names.append(name)

    print(f"[agent_host] enabled sub-agents: {', '.join(enabled_names)}", flush=True)

    return ClaudeAgentOptions(
        # Personality + runtime context + injected facts.
        system_prompt=build_system_prompt(store),
        # Pin the model so behavior is reproducible. Resolution order:
        # explicit `model` arg → CLAUDE_MODEL env var → DEFAULT_MODEL.
        # Triggers use the explicit arg to run on a stronger model (Opus)
        # for the brief/weekly-review prompts; the relay inherits the
        # env-or-default path (Sonnet) for cost.
        model=model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL),
        mcp_servers=mcp_servers,
        # Allowlist what tools the agent may call. Anything not listed here
        # is blocked.
        allowed_tools=allowed_tools,
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


async def process_turn(
    client: ClaudeSDKClient,
    store: MemoryStore,
    conversation_id: str,
    user_text: str,
    is_third_party: bool = False,
) -> str:
    """Submit one user turn, archive + audit everything, return assistant text.

    The relay (and scheduler, when it lands) call this directly. It owns the
    whole bookkeeping cycle for a single turn so callers don't have to
    duplicate the archive/audit logic.

    `is_third_party` flows down to `store.append_message` so the row is
    eligible for the group-chat retention purge (ROADMAP M3). The agent's
    own reply (role='assistant') is always user-authored — only the
    incoming `user` message inherits the flag.
    """
    store.append_message(
        conversation_id, "user", user_text, is_third_party=is_third_party,
    )
    store.log_api_event("user_input", user_text, conversation_id)

    await client.query(user_text)

    reply_chunks: list[str] = []
    async for message in client.receive_response():
        text = _extract_text(message)
        tool_calls = _extract_tool_calls(message)

        if text or tool_calls:
            store.append_message(
                conversation_id,
                "assistant",
                text or "",
                tool_calls=tool_calls or None,
            )
            if text:
                store.log_api_event("assistant_text", text, conversation_id)
                reply_chunks.append(text)
            for tc in tool_calls:
                store.log_api_event("tool_use", tc, conversation_id)

        if isinstance(message, ResultMessage):
            meta: dict[str, Any] = {}
            for attr in ("total_cost_usd", "duration_ms", "num_turns"):
                v = getattr(message, attr, None)
                if v is not None:
                    meta[attr] = v
            usage = getattr(message, "usage", None)
            if usage is not None:
                # ResultMessage.usage is a TypedDict in current SDK builds
                # (hence isinstance(dict) check first). Older or future
                # builds may use an object with __dict__. str() is the
                # last-resort fallback so we capture *something*.
                if isinstance(usage, dict):
                    meta["usage"] = usage
                elif hasattr(usage, "__dict__"):
                    meta["usage"] = usage.__dict__
                else:
                    meta["usage"] = str(usage)
            store.log_api_event("result", str(message), conversation_id, metadata=meta)

    return "\n".join(reply_chunks)


async def process_turn_stream(
    client: ClaudeSDKClient,
    store: MemoryStore,
    conversation_id: str,
    user_text: str,
    is_third_party: bool = False,
):
    """Same as process_turn but yields text chunks as they arrive.

    Used by the web chat SSE endpoint. Archives every message + tool
    call + result event identically to process_turn so the conversation
    archive is consistent across transports. The async generator yields
    dicts with `{"event": "text"|"tool"|"done", ...}` payloads — the
    web route translates them to SSE frames.

    `is_third_party` flows down to archive so the row is eligible for
    the group-chat retention purge (ROADMAP M3).
    """
    store.append_message(
        conversation_id, "user", user_text, is_third_party=is_third_party,
    )
    store.log_api_event("user_input", user_text, conversation_id)

    await client.query(user_text)

    accumulated: list[str] = []
    async for message in client.receive_response():
        text = _extract_text(message)
        tool_calls = _extract_tool_calls(message)

        if text or tool_calls:
            store.append_message(
                conversation_id,
                "assistant",
                text or "",
                tool_calls=tool_calls or None,
            )
            if text:
                store.log_api_event("assistant_text", text, conversation_id)
                accumulated.append(text)
                yield {"event": "text", "text": text}
            for tc in tool_calls:
                store.log_api_event("tool_use", tc, conversation_id)
                yield {"event": "tool", "name": tc.get("name", "?")}

        if isinstance(message, ResultMessage):
            meta: dict[str, Any] = {}
            for attr in ("total_cost_usd", "duration_ms", "num_turns"):
                v = getattr(message, attr, None)
                if v is not None:
                    meta[attr] = v
            usage = getattr(message, "usage", None)
            if usage is not None:
                if isinstance(usage, dict):
                    meta["usage"] = usage
                elif hasattr(usage, "__dict__"):
                    meta["usage"] = usage.__dict__
                else:
                    meta["usage"] = str(usage)
            store.log_api_event("result", str(message), conversation_id, metadata=meta)
            yield {"event": "done", "cost_usd": meta.get("total_cost_usd"), "duration_ms": meta.get("duration_ms")}


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
    options = build_options(store)

    print(
        "personal agent — dev REPL\n"
        f"(conversation_id={conversation_id})\n"
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
                reply = await process_turn(client, store, conversation_id, user_input)
                print(f"agent: {reply}" if reply else "agent: (no text response)")
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
