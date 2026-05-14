"""Discord relay — third transport option alongside iMessage and Telegram.

Bot client listens for DMs from allowlisted user IDs, dispatches each
message through the agent, and replies. Image attachments funnel
through the same vision flow the other transports use.

Setup (web-only, no Mac required):
  1. https://discord.com/developers/applications → New Application →
     name it (e.g. "personal_agent") → Save.
  2. Bot tab → Add Bot → confirm. Then:
     - Toggle "Message Content Intent" ON (privileged — required to
       read DM content)
     - Click "Reset Token" → copy → save as DISCORD_BOT_TOKEN in .env
  3. OAuth2 → URL Generator:
     - Scopes: check `bot`
     - Bot Permissions: Send Messages, Read Message History, Attach Files
     - Copy the generated URL, open it, pick any server you admin (a
       private "personal-agent" server is fine), authorize.
  4. In Discord, enable Developer Mode (User Settings → Advanced →
     Developer Mode on). Right-click yourself → Copy User ID. Save
     this in .env as DISCORD_ALLOWED_USER_IDS (comma-separated to
     allow multiple).
  5. Open a DM with the bot (it'll appear in your DMs once it shares
     a server with you). Say hi.
  6. Set RELAY_TRANSPORT=discord in .env and restart the relay daemon.

Why Discord-bot DMs over channel posts: same identity story as the
Telegram bot — the agent shows up as its own conversation, not a
channel @ mention. Channel support overlaps with the planned
group-chat work in the iMessage relay and is deferred to that.

Outbound (scheduler-driven briefs / reminders) uses Discord's REST
API directly via DiscordSender rather than spinning up the full
client — keeps the per-send latency low.
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

# Late SDK imports so .env is in place first.
from claude_agent_sdk import ClaudeSDKClient  # noqa: E402

from agent_host import build_options, process_turn  # noqa: E402
from memory.store import MemoryStore  # noqa: E402

CONVERSATION_SOURCE = "discord"
CONVERSATION_GAP_HOURS = 4.0
DISCORD_API = "https://discord.com/api/v10"
HTTP_TIMEOUT_S = 20


def _bot_token() -> str:
    t = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not t:
        raise RuntimeError("DISCORD_BOT_TOKEN not set in .env")
    return t


def _allowed_user_ids() -> set[int]:
    raw = os.environ.get("DISCORD_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            print(f"[discord] ignoring non-integer user id {chunk!r}", file=sys.stderr)
    return out


def _resolve_discord_recipient() -> int:
    """Recipient user id for scheduler-driven sends (briefs / reminders).

    For 1:1 DMs the recipient is the principal's user id. Falls back to
    the first allowed user. DISCORD_BRIEF_RECIPIENT_ID overrides if you
    ever want to route briefs to a different account.
    """
    allowed = sorted(_allowed_user_ids())
    if not allowed:
        raise RuntimeError(
            "DISCORD_ALLOWED_USER_IDS not set — needed to know who to "
            "DM for scheduled messages"
        )
    override = os.environ.get("DISCORD_BRIEF_RECIPIENT_ID", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            print(
                f"[discord] DISCORD_BRIEF_RECIPIENT_ID={override!r} isn't an int; "
                "falling back to first allowed user",
                file=sys.stderr,
            )
    return allowed[0]


# ─── Discord REST helpers (used by sender + attachment download) ───────────


def _rest_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bot {_bot_token()}",
        "Content-Type": "application/json",
        "User-Agent": "personal-agent-relay (https://example.invalid, 1.0)",
    }


def _open_dm_channel(recipient_id: int) -> str:
    """Create-or-fetch a DM channel with a user. Returns the channel id."""
    r = requests.post(
        f"{DISCORD_API}/users/@me/channels",
        headers=_rest_headers(),
        json={"recipient_id": str(recipient_id)},
        timeout=HTTP_TIMEOUT_S,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"discord open DM HTTP {r.status_code}: {r.text[:200]}")
    return str(r.json()["id"])


def _rest_post_message(channel_id: str, content: str) -> None:
    r = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=_rest_headers(),
        json={"content": content},
        timeout=HTTP_TIMEOUT_S,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"discord post HTTP {r.status_code}: {r.text[:200]}")


# ─── Sender ─────────────────────────────────────────────────────────────────


class DiscordSender:
    """Sends one Discord DM via REST. Used by both the relay (for replies)
    and the scheduler (briefs / reminders). The DM channel is resolved
    once at construction time and cached."""

    def __init__(self, recipient_id: int) -> None:
        self.recipient_id = recipient_id
        self._channel_id: str | None = None

    def _channel(self) -> str:
        if self._channel_id is None:
            self._channel_id = _open_dm_channel(self.recipient_id)
        return self._channel_id

    def send(self, text: str) -> tuple[bool, str]:
        try:
            # Discord caps single messages at 2000 chars. Split if needed.
            for chunk in _split_for_discord(text):
                _rest_post_message(self._channel(), chunk)
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


def _split_for_discord(text: str, limit: int = 1900) -> list[str]:
    """Discord rejects messages over 2000 chars. Most briefs fit; this is
    just a guard for unusually long ones (e.g. a giant tool result that
    leaked into the brief). Split at line boundaries when possible."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        if used + len(line) > limit and buf:
            out.append("".join(buf))
            buf = [line]
            used = len(line)
        else:
            buf.append(line)
            used += len(line)
    if buf:
        out.append("".join(buf))
    return out


# ─── Attachment download (for inbound vision flow) ─────────────────────────


def _download_attachment(url: str, filename: str) -> tuple[Path | None, str | None]:
    """Pull a Discord CDN URL to a local tempfile. Returns (path, mime)."""
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT_S, stream=True)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[discord] attachment download failed: {e}", file=sys.stderr)
        return None, None
    suffix = Path(filename).suffix or ".bin"
    fd, dst = tempfile.mkstemp(suffix=suffix, prefix="discord_attach_")
    os.close(fd)
    with open(dst, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            f.write(chunk)
    mime = mimetypes.guess_type(dst)[0] or "application/octet-stream"
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
    # Late import — keep discord out of the import graph for non-discord
    # transports.
    import discord  # noqa: E402

    store = MemoryStore()
    allowed = _allowed_user_ids()
    if not allowed:
        print(
            "WARNING: DISCORD_ALLOWED_USER_IDS is empty — bot will ignore everyone. "
            "Add at least your own Discord user id (Developer Mode → right-click "
            "yourself → Copy User ID).",
            file=sys.stderr,
        )

    intents = discord.Intents.default()
    intents.message_content = True  # privileged — must be enabled in portal
    intents.dm_messages = True

    client = discord.Client(intents=intents)
    options = build_options(store)
    sdk_client = ClaudeSDKClient(options=options)
    await sdk_client.__aenter__()

    @client.event
    async def on_ready() -> None:
        print(
            f"[discord] relay started — logged in as {client.user} "
            f"(allowed users: {sorted(allowed)})"
        )

    @client.event
    async def on_message(message) -> None:
        # Ignore self + non-DM channels for v1 (group/channel support
        # is the planned "Group chat in iMessage relay" item).
        if message.author.id == (client.user.id if client.user else 0):
            return
        if message.channel.type != discord.ChannelType.private:
            return
        user_id = message.author.id
        if user_id not in allowed:
            print(
                f"[discord] ignoring DM from unallowed user "
                f"id={user_id} ({message.author.name})"
            )
            return

        # Pull text + image attachments.
        text = message.content or ""
        attachments: list[dict[str, str]] = []
        for att in message.attachments:
            content_type = (att.content_type or "").lower()
            if not content_type.startswith("image/"):
                continue
            path, mime = _download_attachment(att.url, att.filename or "image.bin")
            if path:
                attachments.append({"path": str(path), "mime": mime or content_type})

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
            print(f"[discord] agent error: {e}", file=sys.stderr)
            return

        if not reply:
            print("[discord] no reply from agent")
            return

        # Reply directly via the message's channel (faster than DiscordSender,
        # which is for scheduler-driven sends where we don't have a channel
        # handle).
        try:
            for chunk in _split_for_discord(reply):
                await message.channel.send(chunk)
            print(f"[out] {reply[:20]}")
        except Exception as e:  # noqa: BLE001
            print(f"[discord send failed] {e}", file=sys.stderr)

    try:
        await client.start(_bot_token())
    finally:
        try:
            await sdk_client.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


# ─── Diagnostics ────────────────────────────────────────────────────────────


def _check() -> int:
    print("=== discord relay diagnostics ===\n")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY not set")
        return 1
    print("✓ ANTHROPIC_API_KEY set")

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("✗ DISCORD_BOT_TOKEN not set")
        return 1
    print(f"✓ DISCORD_BOT_TOKEN set (token: {token[:8]}…)")

    allowed = _allowed_user_ids()
    if not allowed:
        print("✗ DISCORD_ALLOWED_USER_IDS empty — bot will ignore everyone")
        return 1
    print(f"✓ DISCORD_ALLOWED_USER_IDS = {sorted(allowed)}")

    try:
        r = requests.get(
            f"{DISCORD_API}/users/@me", headers=_rest_headers(), timeout=10
        )
        r.raise_for_status()
        me = r.json()
        print(f"✓ bot identity: {me.get('username')}#{me.get('discriminator')} ({me.get('id')})")
    except Exception as e:  # noqa: BLE001
        print(f"✗ /users/@me failed: {e}")
        return 1

    print()
    print("All green. Make sure you've enabled the Message Content Intent")
    print("in the Discord Developer Portal, and that the bot has been")
    print("invited to a server you share. Then DM the bot to start chatting.")
    return 0


# ─── Entry point ────────────────────────────────────────────────────────────


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(_check())
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("DISCORD_BOT_TOKEN", "").strip():
        print("error: DISCORD_BOT_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)
    try:
        asyncio.run(_run_daemon())
    except KeyboardInterrupt:
        print("\n[discord] relay stopped.")


if __name__ == "__main__":
    main()
