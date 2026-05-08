"""iMessage relay — the daemon that lets you actually text the agent.

Polls the macOS Messages SQLite database (`~/Library/Messages/chat.db`)
on a configurable cadence, picks up new incoming messages from the
configured contact, feeds them into the agent, and sends the reply back
via AppleScript.

Two macOS permissions are required, granted once per machine:

  1. Full Disk Access — for the process running this daemon (your
     Terminal during dev, or the LaunchAgent helper in production).
     Required to read chat.db.
       System Settings → Privacy & Security → Full Disk Access.

  2. Automation → Messages — granted on the first AppleScript send
     (macOS prompts).

Run modes:
    python -m relay.imessage_relay --check    # diagnostics only, no daemon
    python -m relay.imessage_relay            # run the daemon

Configuration via .env:
    TARGET_PHONE_NUMBER     The contact whose iMessages get relayed.
                            Format: +15555551234  or  apple-id@icloud.com
    IMESSAGE_POLL_INTERVAL  Seconds between chat.db polls. Default 5.
    USER_TIMEZONE           For brief scheduling (used by scheduler too).

Conversation grouping (per project decision): a 4+ hour gap of silence
opens a new conversation row in the archive. The SDK session is kept
intact across the gap so the agent doesn't lose immediate context;
grouping is purely for archive retrieval.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import applescript
from dotenv import load_dotenv

load_dotenv()

# Imports below this line are intentionally late so .env is in place first
# (the SDK reads ANTHROPIC_API_KEY at construction time).

from claude_agent_sdk import ClaudeSDKClient  # noqa: E402

from agent_host import build_options, process_turn  # noqa: E402
from memory.store import MemoryStore  # noqa: E402

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
LAST_SEEN_KEY = "imessage_last_seen_rowid"
CONVERSATION_SOURCE = "imessage"
CONVERSATION_GAP_HOURS = 4.0

# chat.db stores `date` as nanoseconds since 2001-01-01 UTC. Constant for
# converting to/from a real datetime. Used only when we need timestamps.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _apple_ns_to_dt(ns: int) -> datetime:
    return APPLE_EPOCH + timedelta(microseconds=ns / 1000)


# ─── Reading from chat.db ────────────────────────────────────────────────────


class ChatReader:
    """Pulls new incoming messages from a single 1:1 conversation.

    Group chats are out of scope for v1. We filter strictly to messages
    where the sender's `handle.id` matches `target_handle` and `is_from_me=0`.
    """

    def __init__(self, target_handle: str) -> None:
        self.target_handle = target_handle

    def _connect(self) -> sqlite3.Connection:
        if not CHAT_DB.exists():
            raise FileNotFoundError(f"Messages db not found at {CHAT_DB}")
        # Read-only URI so we never accidentally mutate Messages state.
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def can_read(self) -> tuple[bool, str]:
        """Diagnostic: is chat.db readable? Returns (ok, reason_if_not)."""
        try:
            conn = self._connect()
            conn.execute("SELECT 1 FROM message LIMIT 1").fetchone()
            conn.close()
            return True, ""
        except sqlite3.OperationalError as e:
            # Most common cause: missing Full Disk Access permission.
            return False, f"sqlite error: {e} (likely missing Full Disk Access)"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def fetch_new_since(self, last_rowid: int, limit: int = 50) -> list[dict[str, Any]]:
        """Return incoming messages with ROWID > last_rowid, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT m.ROWID AS rowid, m.text, m.date, m.is_from_me, h.id AS sender
                     FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                    WHERE m.is_from_me = 0
                      AND h.id = ?
                      AND m.ROWID > ?
                 ORDER BY m.ROWID ASC
                    LIMIT ?""",
                (self.target_handle, last_rowid, limit),
            ).fetchall()
        return [
            {
                "rowid": int(r["rowid"]),
                "text": r["text"] or "",
                "sent_at": _apple_ns_to_dt(int(r["date"])).isoformat()
                if r["date"]
                else None,
                "sender": r["sender"],
            }
            for r in rows
        ]

    def latest_rowid(self) -> int:
        """Return the highest message ROWID currently in chat.db.

        Used on first run so we don't replay every message ever sent — we
        start the relay from "now".
        """
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(ROWID) AS m FROM message").fetchone()
        return int(row["m"] or 0)


# ─── Sending via AppleScript ─────────────────────────────────────────────────


class ChatSender:
    """Sends one iMessage at a time via AppleScript."""

    def __init__(self, target_handle: str) -> None:
        self.target_handle = target_handle

    def _script(self, text: str) -> str:
        # Escape backslashes first, then double-quotes — order matters.
        safe = text.replace("\\", "\\\\").replace('"', '\\"')
        return f"""
        tell application "Messages"
            set targetService to 1st service whose service type = iMessage
            set targetBuddy to buddy "{self.target_handle}" of targetService
            send "{safe}" to targetBuddy
        end tell
        """

    def send(self, text: str) -> tuple[bool, str]:
        """Send `text` via iMessage. Returns (ok, error_message_if_not)."""
        try:
            applescript.AppleScript(source=self._script(text)).run()
            return True, ""
        except applescript.ScriptError as e:
            return False, f"AppleScript error: {e}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


# ─── The daemon ──────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_daemon(target_handle: str, poll_interval: float) -> None:
    store = MemoryStore()
    reader = ChatReader(target_handle)
    sender = ChatSender(target_handle)

    # On a fresh install, start from "now" — don't replay history.
    last_seen_str = store.get_state(LAST_SEEN_KEY)
    if last_seen_str is None:
        starting_rowid = reader.latest_rowid()
        store.set_state(LAST_SEEN_KEY, str(starting_rowid))
        last_seen = starting_rowid
        print(f"first run — starting from rowid {starting_rowid}")
    else:
        last_seen = int(last_seen_str)
        print(f"resuming from rowid {last_seen}")

    options = build_options(store)
    print(
        f"relay started for {target_handle} (poll every {poll_interval}s). "
        "ctrl-c to stop."
    )

    # One long-running SDK session for the whole relay process. Conversation
    # rollover (4h gap) updates only the archive's conversation_id; the SDK
    # client keeps full immediate context across the gap.
    async with ClaudeSDKClient(options=options) as client:
        conversation_id = store.resume_or_open_conversation(
            source=CONVERSATION_SOURCE,
            gap_threshold_hours=CONVERSATION_GAP_HOURS,
            metadata={"handle": target_handle},
        )
        while True:
            try:
                new_msgs = reader.fetch_new_since(last_seen)
            except Exception as e:  # noqa: BLE001
                print(f"[reader error] {e}", file=sys.stderr)
                await asyncio.sleep(poll_interval)
                continue

            for msg in new_msgs:
                if not msg["text"].strip():
                    last_seen = msg["rowid"]
                    store.set_state(LAST_SEEN_KEY, str(last_seen))
                    continue

                # 4h-gap rollover check. We only roll the archive's
                # conversation_id — the SDK session is left alone.
                conversation_id = store.resume_or_open_conversation(
                    source=CONVERSATION_SOURCE,
                    gap_threshold_hours=CONVERSATION_GAP_HOURS,
                    metadata={"handle": target_handle},
                )

                print(f"[in @ {_now_iso()}] {msg['text'][:80]}")
                try:
                    reply = await process_turn(
                        client, store, conversation_id, msg["text"]
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"[agent error] {e}", file=sys.stderr)
                    last_seen = msg["rowid"]
                    store.set_state(LAST_SEEN_KEY, str(last_seen))
                    continue

                if reply:
                    ok, err = sender.send(reply)
                    if ok:
                        print(f"[out] {reply[:80]}")
                    else:
                        print(f"[send failed] {err}", file=sys.stderr)
                else:
                    print("[no reply]")

                last_seen = msg["rowid"]
                store.set_state(LAST_SEEN_KEY, str(last_seen))

            await asyncio.sleep(poll_interval)


# ─── Diagnostics ─────────────────────────────────────────────────────────────


def _check(target_handle: str | None) -> int:
    print("=== iMessage relay diagnostics ===\n")

    if not target_handle:
        print("✗ TARGET_PHONE_NUMBER is not set in .env")
        return 1
    print(f"✓ TARGET_PHONE_NUMBER = {target_handle}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY is not set in .env")
        return 1
    print("✓ ANTHROPIC_API_KEY set")

    if not CHAT_DB.exists():
        print(f"✗ chat.db not found at {CHAT_DB}")
        return 1

    reader = ChatReader(target_handle)
    ok, why = reader.can_read()
    if not ok:
        print(f"✗ cannot read chat.db: {why}")
        print(
            "\n  fix: System Settings → Privacy & Security → Full Disk Access "
            "→ enable for your terminal app (or the LaunchAgent helper)."
        )
        return 1
    print(f"✓ chat.db readable at {CHAT_DB}")
    print(f"  current latest rowid: {reader.latest_rowid()}")

    # AppleScript permission isn't checkable without trying to send. We
    # don't actually send a probe — the first real send will trigger the
    # macOS prompt if needed.
    print(
        "✓ AppleScript send is not pre-validated; macOS will prompt the first "
        "time you actually run the daemon and it tries to send."
    )

    print("\nall green. you can run the daemon with:")
    print("  python -m relay.imessage_relay")
    return 0


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    target_handle = os.environ.get("TARGET_PHONE_NUMBER", "").strip()
    poll_interval = float(os.environ.get("IMESSAGE_POLL_INTERVAL", "5"))

    if "--check" in sys.argv:
        sys.exit(_check(target_handle))

    if not target_handle:
        print("error: TARGET_PHONE_NUMBER not set in .env", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(_run_daemon(target_handle, poll_interval))
    except KeyboardInterrupt:
        print("\nrelay stopped.")


if __name__ == "__main__":
    main()
