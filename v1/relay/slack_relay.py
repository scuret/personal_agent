"""Slack relay — fourth transport option.

Slack-bolt's Socket Mode keeps the daemon fully local — Slack opens a
WebSocket back to us when a message lands, no public webhook URL
needed. Matches the Telegram long-polling pattern in spirit.

Setup (web-only, no Mac required):
  1. https://api.slack.com/apps → Create New App → "From scratch" →
     name "personal_agent" → pick a workspace you control.
  2. Socket Mode → Enable → name the app-level token (e.g.
     "personal_agent_socket") → grant scope `connections:write`.
     Copy the resulting token (xapp-...). Save as
     SLACK_APP_TOKEN in .env.
  3. OAuth & Permissions → Bot Token Scopes → add:
        chat:write       (send messages)
        im:history       (read DMs)
        im:read          (list DMs)
        files:read       (download image attachments)
        users:read       (resolve user names)
     Install to Workspace. Copy the Bot User OAuth Token (xoxb-...).
     Save as SLACK_BOT_TOKEN in .env.
  4. Event Subscriptions → Enable Events → Subscribe to bot events:
        message.im     (DMs to the bot)
     Save.
  5. Find your Slack user id: workspace → click your name → "View full
     profile" → ⋯ → "Copy member ID" (Uxxxxxxxx). Save as
     SLACK_ALLOWED_USER_IDS in .env (comma-separated for multiple).
  6. Set RELAY_TRANSPORT=slack and restart the relay daemon.

Outbound (scheduler briefs / reminders) goes via WebClient
chat_postMessage to the user's DM channel — same auth, no extra setup.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

from claude_agent_sdk import ClaudeSDKClient  # noqa: E402

from agent_host import build_options, process_turn  # noqa: E402
from memory.store import MemoryStore  # noqa: E402

CONVERSATION_SOURCE = "slack"
CONVERSATION_GAP_HOURS = 4.0
HTTP_TIMEOUT_S = 20


def _bot_token() -> str:
    t = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not t:
        raise RuntimeError("SLACK_BOT_TOKEN not set in .env")
    return t


def _app_token() -> str:
    t = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not t:
        raise RuntimeError("SLACK_APP_TOKEN not set in .env (needed for Socket Mode)")
    return t


def _allowed_user_ids() -> set[str]:
    raw = os.environ.get("SLACK_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    return {chunk.strip() for chunk in raw.split(",") if chunk.strip()}


def _resolve_slack_recipient() -> str:
    """Default DM recipient for scheduler-driven sends. First allowed user
    unless SLACK_BRIEF_USER_ID overrides."""
    allowed = sorted(_allowed_user_ids())
    if not allowed:
        raise RuntimeError(
            "SLACK_ALLOWED_USER_IDS not set — needed to know who to DM "
            "for scheduled messages"
        )
    override = os.environ.get("SLACK_BRIEF_USER_ID", "").strip()
    return override or allowed[0]


# ─── Sender ─────────────────────────────────────────────────────────────────


class SlackSender:
    """Outbound DM via WebClient.chat_postMessage. The DM channel is
    resolved once (conversations.open) and cached."""

    def __init__(self, recipient_user_id: str) -> None:
        from slack_sdk import WebClient  # late import

        self.recipient = recipient_user_id
        self.client = WebClient(token=_bot_token())
        self._channel_id: str | None = None

    def _channel(self) -> str:
        if self._channel_id is None:
            resp = self.client.conversations_open(users=self.recipient)
            self._channel_id = resp["channel"]["id"]
        return self._channel_id

    def send(self, text: str) -> tuple[bool, str]:
        try:
            self.client.chat_postMessage(channel=self._channel(), text=text)
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


# ─── Attachment download (for inbound vision flow) ─────────────────────────


def _download_slack_file(file_info: dict[str, Any]) -> tuple[Path | None, str | None]:
    """Slack file URLs require the bot token in the Authorization header."""
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        return None, None
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {_bot_token()}"},
            timeout=HTTP_TIMEOUT_S,
            stream=True,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[slack] attachment download failed: {e}", file=sys.stderr)
        return None, None
    name = file_info.get("name") or "image.bin"
    suffix = Path(name).suffix or ".bin"
    fd, dst = tempfile.mkstemp(suffix=suffix, prefix="slack_attach_")
    os.close(fd)
    with open(dst, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            f.write(chunk)
    mime = file_info.get("mimetype") or mimetypes.guess_type(dst)[0] or "application/octet-stream"
    return Path(dst), mime


def _format_message_for_agent(text: str, attachments: list[dict[str, str]]) -> str:
    cleaned = (text or "").strip()
    if not attachments:
        return cleaned
    lines = [
        f"[attachment: image at {a['path']} ({a['mime']})]" for a in attachments
    ]
    body = cleaned if cleaned else "(no caption)"
    return "\n".join(lines) + "\n" + body


# ─── Daemon ─────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_daemon() -> None:
    # Late imports — only paid when slack is the active transport.
    from slack_bolt.async_app import AsyncApp  # noqa: E402
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler  # noqa: E402

    store = MemoryStore()
    allowed = _allowed_user_ids()
    if not allowed:
        print(
            "WARNING: SLACK_ALLOWED_USER_IDS is empty — bot will ignore everyone. "
            "Add at least your Slack user id (workspace profile → ⋯ → Copy member ID).",
            file=sys.stderr,
        )

    app = AsyncApp(token=_bot_token())
    options = build_options(store)
    sdk_client = ClaudeSDKClient(options=options)
    await sdk_client.__aenter__()

    @app.event("message")
    async def handle_message(event: dict[str, Any], say) -> None:
        # Only handle DMs (channel type 'im'); ignore message_changed /
        # message_deleted / bot_message subtypes.
        if event.get("channel_type") != "im":
            return
        if event.get("subtype"):
            return
        if event.get("bot_id"):  # skip our own messages
            return

        user_id = event.get("user", "")
        if user_id not in allowed:
            print(f"[slack] ignoring DM from unallowed user id={user_id}")
            return

        text = event.get("text") or ""
        attachments: list[dict[str, str]] = []
        for f in event.get("files") or []:
            mime = (f.get("mimetype") or "").lower()
            if not mime.startswith("image/"):
                continue
            path, resolved_mime = _download_slack_file(f)
            if path:
                attachments.append({"path": str(path), "mime": resolved_mime or mime})

        if not text.strip() and not attachments:
            return

        conversation_id = store.resume_or_open_conversation(
            source=CONVERSATION_SOURCE,
            gap_threshold_hours=CONVERSATION_GAP_HOURS,
            metadata={"user_id": user_id},
        )

        final_text = _format_message_for_agent(text, attachments)
        print(f"[in @ {_now_iso()}] u={user_id}: {final_text[:20]}")
        try:
            reply = await process_turn(sdk_client, store, conversation_id, final_text)
        except Exception as e:  # noqa: BLE001
            print(f"[slack] agent error: {e}", file=sys.stderr)
            return

        if not reply:
            print("[slack] no reply from agent")
            return

        try:
            await say(text=reply)
            print(f"[out] {reply[:20]}")
        except Exception as e:  # noqa: BLE001
            print(f"[slack send failed] {e}", file=sys.stderr)

    print(f"[slack] relay started (allowed users: {sorted(allowed)}). ctrl-c to stop.")
    handler = AsyncSocketModeHandler(app, _app_token())
    try:
        await handler.start_async()
    finally:
        try:
            await sdk_client.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


# ─── Diagnostics ────────────────────────────────────────────────────────────


def _check() -> int:
    print("=== slack relay diagnostics ===\n")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY not set")
        return 1
    print("✓ ANTHROPIC_API_KEY set")

    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        v = os.environ.get(var, "").strip()
        if not v:
            print(f"✗ {var} not set")
            return 1
        print(f"✓ {var} set ({v[:8]}…)")

    allowed = _allowed_user_ids()
    if not allowed:
        print("✗ SLACK_ALLOWED_USER_IDS empty — bot will ignore everyone")
        return 1
    print(f"✓ SLACK_ALLOWED_USER_IDS = {sorted(allowed)}")

    try:
        from slack_sdk import WebClient

        client = WebClient(token=_bot_token())
        resp = client.auth_test()
        print(f"✓ bot identity: @{resp['user']} (team {resp['team']})")
    except Exception as e:  # noqa: BLE001
        print(f"✗ auth.test failed: {e}")
        return 1

    print()
    print("All green. Open the workspace, DM the bot, say hi.")
    return 0


# ─── Entry point ────────────────────────────────────────────────────────────


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(_check())
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        if not os.environ.get(var, "").strip():
            print(f"error: {var} not set in .env", file=sys.stderr)
            sys.exit(1)
    try:
        asyncio.run(_run_daemon())
    except KeyboardInterrupt:
        print("\n[slack] relay stopped.")


if __name__ == "__main__":
    main()
