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

# Mode dispatch. "contact" filters on a single SENDER handle (incoming from
# someone else). "self" listens to your own messages in note-to-self chats
# (is_from_me=1) so you can text your own agent.
MODE_CONTACT = "contact"
MODE_SELF = "self"

# Outgoing-message marker. Every message the relay sends via AppleScript
# is prefixed with U+200B (zero-width space). On read, the relay skips
# messages starting with this character so its own replies don't trigger
# another agent turn. Invisible in iMessage UI; would only matter if you
# copy-pasted a reply and re-sent it (the relay would then skip your
# resend, which is a fine corner-case behavior).
OUTGOING_MARKER = "​"

# chat.db stores `date` as nanoseconds since 2001-01-01 UTC. Constant for
# converting to/from a real datetime. Used only when we need timestamps.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _apple_ns_to_dt(ns: int) -> datetime:
    return APPLE_EPOCH + timedelta(microseconds=ns / 1000)


def _decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract plain text from a Messages attributedBody binary blob.

    chat.db stores message text in two columns: `text` (plain) and
    `attributedBody` (a serialized NSAttributedString in Apple's "streamtyped"
    NSArchiver format). Most of the time both are populated, but when iOS
    Focus / DND is on, iCloud syncs the row to the Mac with `text=NULL`
    while the actual content is preserved in `attributedBody`. Without
    decoding it we'd skip those messages as "empty" and never reply.

    Uses pyobjc's NSUnarchiver (deprecated but still functional on current
    macOS) — already available via the py-applescript dependency.
    """
    if not blob:
        return None
    try:
        # Imported lazily so the module stays importable in non-macOS test
        # contexts even though the daemon as a whole is macOS-only.
        import Foundation  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        ns_data = Foundation.NSData.dataWithBytes_length_(blob, len(blob))
        unarchiver = Foundation.NSUnarchiver.alloc().initForReadingWithData_(ns_data)
        if unarchiver is None:
            return None
        obj = unarchiver.decodeObject()
        if obj is None:
            return None
        # NSAttributedString.string() returns the plain text without
        # formatting attributes.
        if hasattr(obj, "string"):
            return str(obj.string())
        return str(obj)
    except Exception:  # noqa: BLE001 — many failure modes; fall back to None
        return None


def _resolve_text(row: sqlite3.Row) -> str:
    """Pull the message text out of a chat.db row, falling back to attributedBody.

    Order of precedence:
      1. `text` column if non-empty
      2. NSAttributedString decoded from `attributedBody` if non-empty
      3. empty string

    The fallback exists because iOS Focus / DND causes iCloud to deliver
    the row to the Mac with `text=NULL` while the actual content sits in
    `attributedBody`. Without this we'd silently drop everything the user
    sends from a Focus-active iPhone.
    """
    plain = (row["text"] or "").strip()
    if plain:
        return row["text"]
    blob = row["attributedBody"] if "attributedBody" in row.keys() else None
    if blob:
        decoded = _decode_attributed_body(bytes(blob))
        if decoded:
            return decoded
    return ""


# iMessage uses U+FFFC (object replacement character) as an inline
# placeholder where an attachment sits in the text. We strip it before
# sending to the agent — actual attachment paths are surfaced separately
# via the [attachment: ...] marker block.
_OBJ_REPLACEMENT = "￼"


def _format_message_for_agent(text: str, attachments: list[dict[str, str]]) -> str:
    """Render the user's iMessage as the string we feed to the agent.

    If the message has image attachments, a marker block is prepended:

        [attachment: image at /path/to/file.jpg (image/jpeg)]
        [attachment: image at /path/to/file2.heic (image/heic)]
        <user's caption text, or "(no caption)" if empty>

    The agent's personality prompt explains this convention and tells
    it to call mcp__vision__analyze_image with the path.
    """
    cleaned = (text or "").replace(_OBJ_REPLACEMENT, "").strip()
    if not attachments:
        return cleaned

    lines = [
        f"[attachment: image at {a['path']} ({a['mime']})]" for a in attachments
    ]
    body = cleaned if cleaned else "(no caption)"
    return "\n".join(lines) + "\n" + body


def _self_handles() -> list[str]:
    """Resolve the list of "self handles" used in self-mode.

    Includes TARGET_PHONE_NUMBER (always, since in self-mode that's your
    own primary number) and any extras from SELF_HANDLES (comma-sep).
    Empty values are dropped.
    """
    handles = [os.environ.get("TARGET_PHONE_NUMBER", "").strip()]
    extra = os.environ.get("SELF_HANDLES", "").strip()
    if extra:
        handles.extend(h.strip() for h in extra.split(","))
    return [h for h in handles if h]


# ─── Reading from chat.db ────────────────────────────────────────────────────


class ChatReader:
    """Pulls new agent-input messages from chat.db.

    Two modes:

      contact mode (current default)
          Listen for incoming messages from a single sender. Filter:
          h.id = target_handle, is_from_me = 0. Group chats out of scope.

      self mode
          Listen for messages in note-to-self chats so you can text your
          own agent. We do NOT filter on is_from_me because the same
          Apple ID can produce both is_from_me=1 (typed on this Mac) and
          is_from_me=0 (typed on iPhone — Mac sees it as inbound from
          another device on the account). Loop prevention is handled by
          OUTGOING_MARKER (zero-width space) on every reply we send;
          we drop any incoming message whose text starts with it.
    """

    def __init__(self, mode: str, target_handle: str, self_handles: list[str]) -> None:
        if mode not in (MODE_CONTACT, MODE_SELF):
            raise ValueError(f"unknown IMESSAGE_MODE: {mode!r}")
        self.mode = mode
        self.target_handle = target_handle
        self.self_handles = self_handles

    def _connect(self) -> sqlite3.Connection:
        if not CHAT_DB.exists():
            raise FileNotFoundError(f"Messages db not found at {CHAT_DB}")
        # Read-only URI so we never accidentally mutate Messages state.
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _image_attachments_for(self, message_rowid: int) -> list[dict[str, str]]:
        """Return [{path, mime}] for image attachments on a message.

        Only image MIME types are returned; videos/PDFs/other binary types
        are filtered out (vision tool only handles images, and surfacing
        non-image paths to the agent would just confuse it).
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT a.filename, a.mime_type
                     FROM attachment a
                     JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
                    WHERE maj.message_id = ?
                      AND a.mime_type LIKE 'image/%'
                      AND a.filename IS NOT NULL""",
                (message_rowid,),
            ).fetchall()
        # Expand `~/` paths so the vision tool gets a usable absolute path.
        # Filter out files that don't exist (transfer might still be in-flight
        # — we'd rather skip than feed the agent a broken path).
        from os.path import expanduser
        results = []
        for r in rows:
            path = expanduser(r["filename"])
            if Path(path).is_file():
                results.append({"path": path, "mime": r["mime_type"]})
        return results

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

    def fetch_new_since(self, last_rowid: int, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        """Return (processable_messages, max_rowid_seen).

        The second element is the highest ROWID encountered in the query
        window, even for rows we filter out (empty pseudo-messages, our
        own outgoing replies). The daemon uses it to advance `last_seen`
        past skipped rows so they don't get re-fetched forever.
        """
        if self.mode == MODE_CONTACT:
            return self._fetch_contact(last_rowid, limit)
        return self._fetch_self(last_rowid, limit)

    def _fetch_contact(
        self, last_rowid: int, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT m.ROWID AS rowid, m.text, m.attributedBody, m.date, h.id AS sender
                     FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                    WHERE m.is_from_me = 0
                      AND h.id = ?
                      AND m.ROWID > ?
                 ORDER BY m.ROWID ASC
                    LIMIT ?""",
                (self.target_handle, last_rowid, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        max_rowid = last_rowid
        for r in rows:
            max_rowid = max(max_rowid, int(r["rowid"]))
            text = _resolve_text(r)
            attachments = self._image_attachments_for(int(r["rowid"]))
            # Image-only messages have empty text but non-empty attachments;
            # we still want to process those (the agent should describe them).
            if not text.strip() and not attachments:
                print(
                    f"[skipped: empty text from {r['sender']} (rowid={r['rowid']})]",
                    flush=True,
                )
                continue
            final = _format_message_for_agent(text, attachments)
            result.append(self._row_to_dict(r, final))
        return result, max_rowid

    def _fetch_self(
        self, last_rowid: int, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        if not self.self_handles:
            return [], last_rowid
        placeholders = ",".join("?" * len(self.self_handles))
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT m.ROWID AS rowid, m.text, m.attributedBody, m.date,
                            c.chat_identifier AS sender
                     FROM message m
                     JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                     JOIN chat c ON c.ROWID = cmj.chat_id
                    WHERE c.chat_identifier IN ({placeholders})
                      AND m.ROWID > ?
                 ORDER BY m.ROWID ASC
                    LIMIT ?""",
                (*self.self_handles, last_rowid, limit),
            ).fetchall()
        # We treat any non-empty message in a self-handle chat as user
        # input, regardless of is_from_me. That picks up both Mac-typed
        # and iPhone-typed messages. Loop prevention is via OUTGOING_MARKER
        # (a zero-width space prefix on every reply we send).
        #
        # We still report max_rowid across ALL returned rows so last_seen
        # advances past skipped ones; otherwise they'd be re-fetched forever.
        result: list[dict[str, Any]] = []
        max_rowid = last_rowid
        for r in rows:
            max_rowid = max(max_rowid, int(r["rowid"]))
            text = _resolve_text(r)
            if text.startswith(OUTGOING_MARKER):
                continue
            attachments = self._image_attachments_for(int(r["rowid"]))
            # Image-only messages have empty text but non-empty attachments;
            # we still want to process those.
            if not text.strip() and not attachments:
                print(
                    f"[skipped: empty text in chat={r['sender']} (rowid={r['rowid']})]",
                    flush=True,
                )
                continue
            final = _format_message_for_agent(text, attachments)
            result.append(self._row_to_dict(r, final))
        return result, max_rowid

    @staticmethod
    def _row_to_dict(r: sqlite3.Row, text: str) -> dict[str, Any]:
        return {
            "rowid": int(r["rowid"]),
            "text": text,
            "sent_at": _apple_ns_to_dt(int(r["date"])).isoformat() if r["date"] else None,
            "sender": r["sender"],
        }

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
        """Send `text` via iMessage. Returns (ok, error_message_if_not).

        Outgoing text is prefixed with OUTGOING_MARKER (zero-width space)
        so ChatReader's self-mode filter can ignore the relay's own
        replies and avoid an infinite loop. Invisible to the human reader.
        """
        try:
            applescript.AppleScript(source=self._script(OUTGOING_MARKER + text)).run()
            return True, ""
        except applescript.ScriptError as e:
            return False, f"AppleScript error: {e}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


# ─── The daemon ──────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _run_daemon(
    mode: str,
    target_handle: str,
    self_handles: list[str],
    poll_interval: float,
) -> None:
    store = MemoryStore()
    reader = ChatReader(mode=mode, target_handle=target_handle, self_handles=self_handles)
    # In self mode we send replies back to the same self handle the user
    # is texting. In contact mode we send to the contact.
    sender_handle = self_handles[0] if mode == MODE_SELF else target_handle
    sender = ChatSender(sender_handle)

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
    if mode == MODE_SELF:
        watching = f"self mode (chats: {', '.join(self_handles)})"
    else:
        watching = f"contact mode (sender: {target_handle})"
    print(f"relay started — {watching}, poll every {poll_interval}s. ctrl-c to stop.")

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
                new_msgs, max_rowid = reader.fetch_new_since(last_seen)
            except Exception as e:  # noqa: BLE001
                print(f"[reader error] {e}", file=sys.stderr)
                await asyncio.sleep(poll_interval)
                continue

            for msg in new_msgs:
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
                    continue

                if reply:
                    ok, err = sender.send(reply)
                    if ok:
                        print(f"[out] {reply[:80]}")
                    else:
                        print(f"[send failed] {err}", file=sys.stderr)
                else:
                    print("[no reply]")

            # Advance past everything we saw (including filtered-out rows
            # like empty pseudo-messages and our own marker-tagged replies)
            # so they don't get re-fetched forever.
            if max_rowid > last_seen:
                last_seen = max_rowid
                store.set_state(LAST_SEEN_KEY, str(last_seen))

            await asyncio.sleep(poll_interval)


# ─── Diagnostics ─────────────────────────────────────────────────────────────


def _check(mode: str, target_handle: str, self_handles: list[str]) -> int:
    print("=== iMessage relay diagnostics ===\n")

    if mode not in (MODE_CONTACT, MODE_SELF):
        print(f"✗ IMESSAGE_MODE = {mode!r} (must be 'contact' or 'self')")
        return 1
    print(f"✓ IMESSAGE_MODE = {mode}")

    if not target_handle:
        print("✗ TARGET_PHONE_NUMBER is not set in .env")
        return 1
    print(f"✓ TARGET_PHONE_NUMBER = {target_handle}")

    if mode == MODE_SELF:
        print(f"✓ self handles = {self_handles}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY is not set in .env")
        return 1
    print("✓ ANTHROPIC_API_KEY set")

    if not CHAT_DB.exists():
        print(f"✗ chat.db not found at {CHAT_DB}")
        return 1

    reader = ChatReader(mode=mode, target_handle=target_handle, self_handles=self_handles)
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
    mode = os.environ.get("IMESSAGE_MODE", MODE_CONTACT).strip().lower()
    target_handle = os.environ.get("TARGET_PHONE_NUMBER", "").strip()
    self_handles = _self_handles() if mode == MODE_SELF else []
    poll_interval = float(os.environ.get("IMESSAGE_POLL_INTERVAL", "5"))

    if "--check" in sys.argv:
        sys.exit(_check(mode, target_handle, self_handles))

    if not target_handle:
        print("error: TARGET_PHONE_NUMBER not set in .env", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        sys.exit(1)
    if mode == MODE_SELF and not self_handles:
        print("error: IMESSAGE_MODE=self requires at least TARGET_PHONE_NUMBER set", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(_run_daemon(mode, target_handle, self_handles, poll_interval))
    except KeyboardInterrupt:
        print("\nrelay stopped.")


if __name__ == "__main__":
    main()
