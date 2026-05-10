"""Telegram relay — alternative transport to iMessage.

The agent's full feature set (memory, sub-agents, scheduler, vision)
works with this daemon swapped in for the iMessage relay. iMessage and
Telegram never run simultaneously — `RELAY_TRANSPORT` in `.env` picks
which one starts.

Why an alternative transport:
  * iMessage is Apple-only and requires an always-on Mac with chat.db
    + AppleScript permissions. Telegram works from anywhere.
  * Telegram bypasses iOS Focus / DND quirks since it's a different app.
  * Cleaner identity story: the bot is a separate "person" in your
    chat list, not a note-to-self thread.

Setup (web/phone only — fully remote):
  1. In Telegram, search for `@BotFather` and start a chat.
  2. Send `/newbot` → pick a display name → pick a username (must end
     in `bot`, e.g. `personal_agent_for_stephen_bot`).
  3. BotFather replies with a token like `123456:ABC-DEF...`. Save
     this as `TELEGRAM_BOT_TOKEN` in `.env`.
  4. Find your Telegram user ID (numeric). Easiest path:
        Search for `@userinfobot` in Telegram → start it → it replies
        with your numeric ID.
     Add it to `TELEGRAM_ALLOWED_USER_IDS` in `.env` (comma-separated
     for multiple). The bot ignores messages from anyone not in this
     list — without it, anyone who guesses your bot's username can
     talk to it.
  5. Send `/start` to your bot once from your phone so Telegram
     allows it to message you back.
  6. Set `RELAY_TRANSPORT=telegram` in `.env` and run/restart the
     relay daemon.

Long-polling: we use Telegram's `getUpdates?timeout=30` so the daemon
holds the connection open and returns immediately when a message
arrives. Quick latency, low CPU between messages.

Image attachments: when a Telegram message has a photo or document
attachment, the relay calls `getFile` + downloads it to a tempfile and
prepends the same `[attachment: image at PATH (mime)]` marker the
iMessage relay uses, so the agent's vision tool flow is identical
across transports.
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

# State key for the last update_id we processed (persists across restarts).
LAST_UPDATE_KEY = "telegram_last_update_id"
CONVERSATION_SOURCE = "telegram"
CONVERSATION_GAP_HOURS = 4.0
LONG_POLL_TIMEOUT_S = 30
HTTP_TIMEOUT_S = 40  # > LONG_POLL_TIMEOUT_S


def _bot_token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not t:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in .env")
    return t


def _allowed_user_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
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
            print(f"[telegram] ignoring non-integer user id {chunk!r}", file=sys.stderr)
    return out


def _resolve_telegram_chat_id() -> int:
    """Where the scheduler sends scheduled briefs / reminders.

    For 1:1 bot chats, chat_id == user_id, so we use the first allowed
    user id. If you ever extend to group chats, you'd want a separate
    TELEGRAM_BRIEF_CHAT_ID env var.
    """
    allowed = sorted(_allowed_user_ids())
    if not allowed:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_IDS not set — needed to know where to "
            "send scheduled messages"
        )
    override = os.environ.get("TELEGRAM_BRIEF_CHAT_ID", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            print(
                f"[telegram] TELEGRAM_BRIEF_CHAT_ID={override!r} isn't an int; "
                "falling back to first allowed user",
                file=sys.stderr,
            )
    return allowed[0]


# ─── Telegram API wrappers ──────────────────────────────────────────────────


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{_bot_token()}/{method}"


def _file_url(file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{_bot_token()}/{file_path}"


def _api_get(method: str, **params: Any) -> dict[str, Any]:
    r = requests.get(_api_url(method), params=params, timeout=HTTP_TIMEOUT_S)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"telegram API {method} returned not-ok: {body}")
    return body


def _api_post(method: str, **payload: Any) -> dict[str, Any]:
    r = requests.post(_api_url(method), json=payload, timeout=HTTP_TIMEOUT_S)
    if r.status_code >= 400:
        # surface Telegram's error description (often "chat not found",
        # "bot was blocked by the user", etc.)
        try:
            err = r.json().get("description", r.text[:200])
        except ValueError:
            err = r.text[:200]
        raise RuntimeError(f"telegram API {method} HTTP {r.status_code}: {err}")
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"telegram API {method} returned not-ok: {body}")
    return body


# ─── Sender ─────────────────────────────────────────────────────────────────


class TelegramSender:
    """Sends one Telegram message via sendMessage. Used by both the relay
    daemon (for replies) and the scheduler (for briefs / reminders)."""

    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id

    def send(self, text: str) -> tuple[bool, str]:
        try:
            _api_post("sendMessage", chat_id=self.chat_id, text=text)
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


# ─── Attachment download ────────────────────────────────────────────────────


def _download_attachment(file_id: str) -> tuple[Path | None, str | None]:
    """Resolve a Telegram file_id to (local_path, mime_type).

    Returns (None, None) on failure. Saves to a tempfile in the system
    temp dir; we don't bother cleaning up — files are tiny and the OS
    rotates /tmp.
    """
    try:
        body = _api_get("getFile", file_id=file_id)
        info = body.get("result") or {}
        remote_path = info.get("file_path")
        if not remote_path:
            return None, None
        url = _file_url(remote_path)
        r = requests.get(url, timeout=HTTP_TIMEOUT_S, stream=True)
        r.raise_for_status()
    except (requests.RequestException, RuntimeError) as e:
        print(f"[telegram] attachment download failed: {e}", file=sys.stderr)
        return None, None

    suffix = Path(remote_path).suffix or ".bin"
    fd, dst = tempfile.mkstemp(suffix=suffix, prefix="tg_attach_")
    os.close(fd)
    with open(dst, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            f.write(chunk)
    mime = mimetypes.guess_type(dst)[0] or "application/octet-stream"
    return Path(dst), mime


def _format_message_for_agent(text: str, attachments: list[dict[str, str]]) -> str:
    """Same marker convention the iMessage relay uses, for vision parity."""
    cleaned = (text or "").strip()
    if not attachments:
        return cleaned
    lines = [
        f"[attachment: image at {a['path']} ({a['mime']})]" for a in attachments
    ]
    body = cleaned if cleaned else "(no caption)"
    return "\n".join(lines) + "\n" + body


# ─── Message handling ───────────────────────────────────────────────────────


def _extract_text_and_attachments(msg: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """Pull user-typed text + image attachment paths out of a Telegram message."""
    text = msg.get("text") or msg.get("caption") or ""
    attachments: list[dict[str, str]] = []

    # Photos arrive as a list of sizes; the last one is highest resolution.
    photos = msg.get("photo") or []
    if photos:
        biggest = photos[-1]
        path, mime = _download_attachment(biggest.get("file_id", ""))
        if path:
            attachments.append({"path": str(path), "mime": mime or "image/jpeg"})

    # Document attachments — only forward if they're images. Other doc types
    # would need different vision/parsing tools we don't have wired up.
    doc = msg.get("document")
    if doc and (doc.get("mime_type") or "").startswith("image/"):
        path, mime = _download_attachment(doc.get("file_id", ""))
        if path:
            attachments.append({"path": str(path), "mime": mime or doc["mime_type"]})

    return text, attachments


# ─── Daemon ─────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_daemon() -> None:
    store = MemoryStore()
    allowed = _allowed_user_ids()
    if not allowed:
        print(
            "WARNING: TELEGRAM_ALLOWED_USER_IDS is empty — bot will ignore everyone. "
            "Add at least your own Telegram user id (find via @userinfobot).",
            file=sys.stderr,
        )

    # On first run start from the latest update so we don't replay history.
    last_seen_str = store.get_state(LAST_UPDATE_KEY)
    if last_seen_str is None:
        last_update_id = 0
        store.set_state(LAST_UPDATE_KEY, "0")
        print("[telegram] first run — starting from update_id 0")
    else:
        last_update_id = int(last_seen_str)
        print(f"[telegram] resuming from update_id {last_update_id}")

    options = build_options(store)
    print(
        f"[telegram] relay started (allowed users: {sorted(allowed)}). "
        "ctrl-c to stop."
    )

    async with ClaudeSDKClient(options=options) as client:
        while True:
            try:
                body = _api_get(
                    "getUpdates",
                    offset=last_update_id + 1,
                    timeout=LONG_POLL_TIMEOUT_S,
                )
            except (requests.RequestException, RuntimeError) as e:
                print(f"[telegram] getUpdates failed: {e}", file=sys.stderr)
                await asyncio.sleep(5)
                continue

            for update in body.get("result", []):
                upd_id = int(update.get("update_id", 0))
                last_update_id = max(last_update_id, upd_id)
                store.set_state(LAST_UPDATE_KEY, str(last_update_id))

                msg = update.get("message")
                if not isinstance(msg, dict):
                    continue
                user = msg.get("from") or {}
                user_id = user.get("id")
                if user_id not in allowed:
                    print(
                        f"[telegram] ignoring message from unallowed user "
                        f"id={user_id} ({user.get('first_name', '?')})"
                    )
                    continue

                text, attachments = _extract_text_and_attachments(msg)
                if not text.strip() and not attachments:
                    print(f"[telegram] skipping empty message update_id={upd_id}")
                    continue

                # Conversation rollover: 4h-gap rule, same as iMessage relay.
                conversation_id = store.resume_or_open_conversation(
                    source=CONVERSATION_SOURCE,
                    gap_threshold_hours=CONVERSATION_GAP_HOURS,
                    metadata={"user_id": user_id},
                )

                final_text = _format_message_for_agent(text, attachments)
                print(f"[in @ {_now_iso()}] u={user_id}: {final_text[:80]}")
                try:
                    reply = await process_turn(
                        client, store, conversation_id, final_text
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[telegram] agent error: {e}", file=sys.stderr)
                    continue

                if not reply:
                    print("[telegram] no reply from agent")
                    continue

                chat_id = (msg.get("chat") or {}).get("id", user_id)
                sender = TelegramSender(chat_id)
                ok, err = sender.send(reply)
                if ok:
                    print(f"[out] {reply[:80]}")
                else:
                    print(f"[telegram send failed] {err}", file=sys.stderr)


# ─── Diagnostics ────────────────────────────────────────────────────────────


def _check() -> int:
    print("=== telegram relay diagnostics ===\n")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY not set")
        return 1
    print("✓ ANTHROPIC_API_KEY set")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("✗ TELEGRAM_BOT_TOKEN not set")
        return 1
    print(f"✓ TELEGRAM_BOT_TOKEN set (token: {token[:8]}…)")

    allowed = _allowed_user_ids()
    if not allowed:
        print("✗ TELEGRAM_ALLOWED_USER_IDS empty — bot will ignore everyone")
        return 1
    print(f"✓ TELEGRAM_ALLOWED_USER_IDS = {sorted(allowed)}")

    try:
        body = _api_get("getMe")
        bot = body.get("result", {})
        print(f"✓ bot identity: @{bot.get('username')} ({bot.get('first_name')})")
    except Exception as e:  # noqa: BLE001
        print(f"✗ getMe failed: {e}")
        return 1

    print()
    print("All green. Send /start to your bot from a phone signed in as one")
    print("of the allowed user ids, then run:  python -m relay.telegram_relay")
    return 0


# ─── Entry point ────────────────────────────────────────────────────────────


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(_check())
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        print("error: TELEGRAM_BOT_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)
    try:
        asyncio.run(_run_daemon())
    except KeyboardInterrupt:
        print("\n[telegram] relay stopped.")


if __name__ == "__main__":
    main()
