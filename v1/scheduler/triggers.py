"""Trigger scheduler — fires the morning brief and weekly review.

Two scheduled events in v1:

  morning_brief   — daily at the configured time (default 07:30), optionally
                    weekdays-only. Asks the agent for today's calendar, top
                    tasks, and urgent unread emails; sends the result via
                    iMessage to your number.

  weekly_review   — weekly on the configured day (default Sunday) at the
                    configured time (default 20:00). Asks the agent for last
                    week's incomplete tasks and the upcoming week's calendar.

Architecture (per project decision):
  * Separate daemon from the relay. Each is its own process.
  * Uses the same ChatSender (so the zero-width-space marker is set on
    every outgoing message — the relay's reader will skip these and not
    process them as user input if both daemons are running concurrently).
  * Each fire opens a NEW conversation in the archive (source="scheduler")
    so briefings don't get tangled with your live iMessage thread.
  * Schedule config lives in config/triggers.yaml; synthetic prompts live
    in this file so they're easy to tune.

Run modes:
    python -m scheduler.triggers --check       # show next fire times
    python -m scheduler.triggers --run-now <morning_brief|weekly_review>
                                              # fire one trigger immediately
                                              # (useful for testing)
    python -m scheduler.triggers              # run the daemon
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytz
import yaml
from dotenv import load_dotenv

load_dotenv()

# Late imports so .env is in place first.
from claude_agent_sdk import ClaudeSDKClient  # noqa: E402

from agent_host import build_options, process_turn  # noqa: E402
from memory.store import MemoryStore  # noqa: E402
from relay.sender import make_sender  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "triggers.yaml"
CONVERSATION_SOURCE = "scheduler"

# Wake every 30s and check the wallclock. Short enough that we catch
# fires within half a minute even after a Mac sleep + wake; long enough
# to keep the daemon nearly idle between checks.
TICK_SECONDS = 30

# State keys for tracking when each trigger last fired. Looking these up
# against the most recent scheduled time is how we detect "we should
# have fired but didn't" (i.e. Mac slept through 07:30).
def _last_fired_key(trigger_name: str) -> str:
    return f"scheduler_last_fired_{trigger_name}"

# Synthetic prompts keyed by trigger name. The agent's personality + tools
# do the actual work — these prompts just frame the request.
PROMPTS: dict[str, str] = {
    "morning_brief": (
        "Generate the principal's daily brief, sent unprompted. Pull data:\n"
        "  • calendar_list_events for today only\n"
        "  • todoist_list_tasks with filter 'today | overdue'\n"
        "  • gmail_search 'is:unread newer_than:1d' — skim, surface only the "
        "ones that genuinely matter (skip newsletters, receipts, automated alerts)\n"
        "\n"
        "Output budget: target ~200 chars, hard cap ~350. Use **bold** headers "
        "only if you have ≥2 distinct sections. Skip any section that's empty "
        "or boring — don't pad ('calendar's clear today' adds no value if there's "
        "nothing on it; just omit). Surface AT MOST top 3 tasks and AT MOST 1-2 "
        "urgent emails. Never narrate what you're doing. No 'good morning' "
        "preamble. The principal is reading on their phone."
    ),
    "weekly_review": (
        "Generate the principal's Sunday-evening weekly review, sent unprompted. "
        "Pull:\n"
        "  • todoist_list_tasks filter 'overdue' — what slipped last week\n"
        "  • calendar_list_events for the next 7 days — heavy/important days only\n"
        "\n"
        "Output budget: target ~250 chars, hard cap ~400. Use **bold** headers "
        "for the two sections. Skip a section entirely if it's empty (no overdue "
        "tasks → don't say so, just omit). For the upcoming week, flag only days "
        "with heavy load or stuff worth prepping for; don't list every event. "
        "Be honest but brief about slippage. Never narrate. The principal is "
        "reading on their phone."
    ),
}

# Map yaml `day:` strings to Python weekday() integers (Mon=0 .. Sun=6).
_DAY_NAME_TO_WEEKDAY = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"trigger config not found at {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text()) or {}


def _user_tz() -> pytz.BaseTzInfo:
    name = os.environ.get("USER_TIMEZONE", "America/Chicago")
    try:
        return pytz.timezone(name)
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.timezone("America/Chicago")


def _parse_hhmm(s: str) -> dtime:
    h, m = s.strip().split(":")
    return dtime(int(h), int(m))


def _next_morning_brief_fire(cfg: dict[str, Any], now_local: datetime) -> datetime | None:
    """Next future fire — used by --check to show when the user can expect one."""
    if not cfg.get("enabled", False):
        return None
    fire_time = _parse_hhmm(cfg.get("time", "07:30"))
    weekdays_only = bool(cfg.get("weekdays_only", False))

    candidate = now_local.replace(
        hour=fire_time.hour, minute=fire_time.minute, second=0, microsecond=0
    )
    if candidate <= now_local:
        candidate += timedelta(days=1)
    while weekdays_only and candidate.weekday() >= 5:  # Sat=5, Sun=6
        candidate += timedelta(days=1)
    return candidate


def _next_weekly_review_fire(cfg: dict[str, Any], now_local: datetime) -> datetime | None:
    """Next future fire — used by --check to show when the user can expect one."""
    if not cfg.get("enabled", False):
        return None
    fire_time = _parse_hhmm(cfg.get("time", "20:00"))
    target_weekday = _DAY_NAME_TO_WEEKDAY.get(cfg.get("day", "sunday").lower(), 6)

    delta_days = (target_weekday - now_local.weekday()) % 7
    candidate = (now_local + timedelta(days=delta_days)).replace(
        hour=fire_time.hour, minute=fire_time.minute, second=0, microsecond=0
    )
    if candidate <= now_local:
        candidate += timedelta(days=7)
    return candidate


def _last_morning_brief_scheduled(cfg: dict[str, Any], now_local: datetime) -> datetime | None:
    """Most recent moment in the past when this trigger SHOULD have fired.

    Daemon compares this against the persisted "last fired" timestamp: if
    last-fired is older than this, we missed a fire (Mac slept through it,
    or the daemon was off) and should fire now to catch up.
    """
    if not cfg.get("enabled", False):
        return None
    fire_time = _parse_hhmm(cfg.get("time", "07:30"))
    weekdays_only = bool(cfg.get("weekdays_only", False))

    candidate = now_local.replace(
        hour=fire_time.hour, minute=fire_time.minute, second=0, microsecond=0
    )
    if candidate > now_local:
        candidate -= timedelta(days=1)
    while weekdays_only and candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _last_weekly_review_scheduled(cfg: dict[str, Any], now_local: datetime) -> datetime | None:
    """Most recent moment in the past when the weekly review SHOULD have fired."""
    if not cfg.get("enabled", False):
        return None
    fire_time = _parse_hhmm(cfg.get("time", "20:00"))
    target_weekday = _DAY_NAME_TO_WEEKDAY.get(cfg.get("day", "sunday").lower(), 6)

    days_back = (now_local.weekday() - target_weekday) % 7
    candidate = (now_local - timedelta(days=days_back)).replace(
        hour=fire_time.hour, minute=fire_time.minute, second=0, microsecond=0
    )
    if candidate > now_local:
        candidate -= timedelta(days=7)
    return candidate


# Triggers that the daemon checks every tick. Each entry pairs the
# "compute most recent past scheduled time" function with the config
# key in triggers.yaml.
_DAEMON_TRIGGERS: list[tuple[str, Any, str]] = [
    ("morning_brief", _last_morning_brief_scheduled, "morning_brief"),
    ("weekly_review", _last_weekly_review_scheduled, "weekly_review"),
]


def _compute_next_fires(now_local: datetime, config: dict[str, Any]) -> list[tuple[str, datetime]]:
    """For diagnostics: upcoming fire times so --check can show them."""
    fires: list[tuple[str, datetime]] = []
    sched = config.get("scheduled", {})
    if (mb := _next_morning_brief_fire(sched.get("morning_brief", {}), now_local)) is not None:
        fires.append(("morning_brief", mb))
    if (wr := _next_weekly_review_fire(sched.get("weekly_review", {}), now_local)) is not None:
        fires.append(("weekly_review", wr))
    fires.sort(key=lambda x: x[1])
    return fires


# ─── Sending ─────────────────────────────────────────────────────────────────


# ─── Email watch ────────────────────────────────────────────────────────────
#
# Polls Gmail for new unread email and pings the principal when something
# matches the rules in config/triggers.yaml's `email_triggers` block.
# Runs as part of the scheduler tick (no separate daemon) so it's gated
# by both the master `enabled` flag AND a `every_minutes` throttle.


_EMAIL_WATCH_LAST_CHECK_KEY = "email_watch_last_check"
_EMAIL_WATCH_SEEN_KEY = "email_watch_seen_ids"
# Trim the seen-ID set to this size so it doesn't grow forever; emails
# we've already seen don't re-trigger if Gmail's `newer_than:Nh` window
# overlaps with our previous fire.
_EMAIL_WATCH_SEEN_CAP = 200


def _fetch_recent_unread_gmail(limit: int = 50) -> list[dict[str, Any]]:
    """Fetch unread Gmail messages from the last hour, metadata only.

    Uses Gmail's `newer_than:1h` which gives an hour of slop on top of
    our 15-minute (default) tick — emails arriving during a fire are
    still picked up on the next one. Dedup happens in caller via the
    seen-ID set.
    """
    from mcp_servers.google_auth import build_service  # late import — lazy

    svc = build_service("gmail", "v1")
    resp = (
        svc.users()
        .messages()
        .list(userId="me", q="is:unread newer_than:1h", maxResults=limit)
        .execute()
    )
    ids = [m["id"] for m in (resp.get("messages") or [])]
    out: list[dict[str, Any]] = []
    for mid in ids:
        msg = (
            svc.users()
            .messages()
            .get(userId="me", id=mid, format="metadata")
            .execute()
        )
        headers = {
            h["name"]: h["value"] for h in (msg["payload"].get("headers") or [])
        }
        out.append(
            {
                "id": mid,
                "thread_id": msg.get("threadId", ""),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            }
        )
    return out


def _email_matches_rules(
    email: dict[str, Any], senders: list[str], keywords: list[str]
) -> tuple[bool, str]:
    """Return (matches, reason). Case-insensitive substring matching."""
    from_field = (email.get("from") or "").lower()
    subject = (email.get("subject") or "").lower()
    snippet = (email.get("snippet") or "").lower()
    haystack = f"{subject}\n{snippet}"
    for sender in senders:
        s = (sender or "").strip().lower()
        if s and s in from_field:
            return True, f"sender match: {sender}"
    for kw in keywords:
        k = (kw or "").strip().lower()
        if k and k in haystack:
            return True, f"keyword match: {kw}"
    return False, ""


def _short_sender(from_header: str) -> str:
    """Pull a friendly sender name from a 'Display Name <addr@example.com>' header."""
    s = (from_header or "").strip()
    if "<" in s:
        s = s.split("<")[0].strip().strip('"')
    if len(s) > 40:
        s = s[:40] + "…"
    return s or "?"


def _fire_email_watch(store: MemoryStore, config: dict[str, Any], now: datetime) -> None:
    cfg = (config.get("email_triggers") or {})
    if not cfg.get("enabled"):
        return

    every = int(cfg.get("every_minutes", 15))
    last_check_str = store.get_state(_EMAIL_WATCH_LAST_CHECK_KEY)
    if last_check_str:
        try:
            last_check = datetime.fromisoformat(last_check_str)
        except ValueError:
            last_check = None
        if last_check is not None and (now - last_check).total_seconds() < every * 60:
            return  # not yet — throttled

    senders = list(cfg.get("important_senders") or [])
    keywords = list(cfg.get("urgency_keywords") or [])
    if not senders and not keywords:
        # No filters configured — nothing could match. Update state to keep
        # throttling honest, then exit silently.
        store.set_state(_EMAIL_WATCH_LAST_CHECK_KEY, now.isoformat())
        return

    # Load the seen-id set from state.
    try:
        seen_list = json.loads(store.get_state(_EMAIL_WATCH_SEEN_KEY, "[]") or "[]")
        if not isinstance(seen_list, list):
            seen_list = []
    except json.JSONDecodeError:
        seen_list = []
    seen: set[str] = set(seen_list)

    try:
        emails = _fetch_recent_unread_gmail(limit=50)
    except Exception as e:  # noqa: BLE001
        print(f"[email_watch] gmail fetch failed: {e}", file=sys.stderr)
        return

    flagged: list[tuple[dict[str, Any], str]] = []
    for email in emails:
        eid = email["id"]
        if eid in seen:
            continue
        seen.add(eid)
        matched, reason = _email_matches_rules(email, senders, keywords)
        if matched:
            flagged.append((email, reason))

    # Cap and persist seen set.
    if len(seen) > _EMAIL_WATCH_SEEN_CAP:
        seen = set(list(seen)[-_EMAIL_WATCH_SEEN_CAP:])
    store.set_state(_EMAIL_WATCH_SEEN_KEY, json.dumps(list(seen)))
    store.set_state(_EMAIL_WATCH_LAST_CHECK_KEY, now.isoformat())

    if not flagged:
        return

    if len(flagged) == 1:
        email, reason = flagged[0]
        text = (
            f"📧 Important email\n"
            f"from {_short_sender(email['from'])}\n"
            f"subject: {email['subject'] or '(no subject)'}\n"
            f"({reason})"
        )
    else:
        lines = [f"📧 {len(flagged)} important emails:"]
        for email, reason in flagged:
            subj = (email["subject"] or "(no subject)")[:60]
            lines.append(
                f"- {_short_sender(email['from'])}: {subj} ({reason})"
            )
        text = "\n".join(lines)

    sender = make_sender()
    ok, err = sender.send(text)
    if ok:
        print(f"[email_watch] notified — {len(flagged)} flagged email(s)")
    else:
        print(f"[email_watch] send failed: {err}", file=sys.stderr)


def _fire_due_reminders(store: MemoryStore) -> None:
    """Send any pending reminder whose fire_at has passed.

    Runs on every scheduler tick. Reminders are stored with ISO 8601 +
    offset, so we compare against UTC-now in ISO form. The agent
    schedules them via mcp__reminders__remind (one-off) or
    mcp__reminders__remind_recurring.

    For one-off reminders we mark fired_at after a successful send.
    For recurring reminders we advance fire_at to the next occurrence
    and leave fired_at NULL so they keep firing.

    On send error, we leave the reminder in pending state (no advance,
    no fired_at update) so the next tick retries.
    """
    now = datetime.now(timezone.utc)
    due = store.get_due_reminders(before_iso=now.isoformat())
    if not due:
        return
    sender = make_sender()
    for r in due:
        ok, err = sender.send(r["message"])
        if not ok:
            print(f"[reminder send failed] #{r['id']}: {err}", file=sys.stderr)
            continue

        rule_raw = r.get("recurrence_rule")
        if rule_raw:
            try:
                rule = json.loads(rule_raw)
            except (TypeError, ValueError):
                rule = None
            if rule:
                # Lazy import — keeps reminders_server only loaded when needed.
                from mcp_servers.reminders_server import _next_recurrence

                next_fire = _next_recurrence(rule, now)
                if next_fire is not None:
                    store.advance_reminder_fire_at(r["id"], next_fire.isoformat())
                    print(
                        f"[reminder fired] #{r['id']} (recurring → next "
                        f"{next_fire.strftime('%Y-%m-%d %H:%M %Z')}): "
                        f"{r['message'][:80]}"
                    )
                    continue
            # Bad/unparseable rule — fall through to one-off marking so we
            # don't loop forever on the same row.
            print(
                f"[reminder warn] #{r['id']} has unparseable recurrence_rule; "
                "treating as one-off",
                file=sys.stderr,
            )

        store.mark_reminder_fired(r["id"])
        print(f"[reminder fired] #{r['id']}: {r['message'][:80]}")


async def _fire_trigger(trigger_name: str) -> None:
    """Generate the brief and send it via the active transport.

    `make_sender()` picks iMessage or Telegram based on RELAY_TRANSPORT.
    One conversation row per fire.
    """
    prompt = PROMPTS.get(trigger_name)
    if not prompt:
        print(f"[fire] unknown trigger: {trigger_name}", file=sys.stderr)
        return

    store = MemoryStore()
    sender = make_sender()
    options = build_options(store)

    conversation_id = store.open_conversation(
        source=CONVERSATION_SOURCE, metadata={"trigger": trigger_name}
    )
    print(f"[fire @ {datetime.now().isoformat()}] {trigger_name} (conv={conversation_id})")

    try:
        async with ClaudeSDKClient(options=options) as client:
            reply = await process_turn(client, store, conversation_id, prompt)
    finally:
        store.close_conversation(conversation_id)

    if not reply:
        print(f"[fire] {trigger_name} produced no text — nothing to send")
        return

    ok, err = sender.send(reply)
    if ok:
        print(f"[sent] {trigger_name}: {reply[:80]}")
    else:
        print(f"[send failed] {err}", file=sys.stderr)


# ─── Daemon ──────────────────────────────────────────────────────────────────


def _most_recent_archive_fire(store: MemoryStore, trigger_name: str) -> datetime | None:
    """Find the most recent time this trigger actually fired, from the archive.

    Each fire opens a conversation with source='scheduler' and metadata
    containing trigger=<name>. The conversation's started_at is when we
    fired. Returns a UTC-aware datetime, or None if no fire is recorded.
    """
    rows = store._conn().execute(  # noqa: SLF001 — store has no public query
        """SELECT started_at, metadata
             FROM conversations
            WHERE source = 'scheduler'
         ORDER BY started_at DESC LIMIT 50""",
    ).fetchall()
    for r in rows:
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except json.JSONDecodeError:
            continue
        if meta.get("trigger") == trigger_name:
            try:
                return datetime.fromisoformat(r["started_at"])
            except ValueError:
                return None
    return None


async def _run_daemon() -> None:
    """Wallclock-based scheduler loop.

    Wakes every TICK_SECONDS, compares wallclock to the most recent past
    scheduled time of each trigger, and fires any whose last-fired
    timestamp predates that scheduled moment. This pattern survives macOS
    sleep: when the Mac wakes up, the next tick observes that 07:30 has
    passed without firing and catches up immediately.

    Startup priming logic:
      * If we have a last-fired record, use it (catchup logic in the loop
        will fire if a scheduled time has passed since).
      * If no last-fired record exists but the archive shows a recent fire
        (within 7 days), backfill last-fired from that — so a daemon
        restart after the schema added the state KV doesn't lose history,
        and a Mac that was off through a fire window catches up on wake.
      * Otherwise (truly fresh install with no history), prime to "now"
        so we don't fire stale briefs at install time.
    """
    config = _load_config()
    tz = _user_tz()
    store = MemoryStore()

    print(f"scheduler started (tz={tz.zone}, tick every {TICK_SECONDS}s). ctrl-c to stop.")

    now = datetime.now(tz)
    for name, _, _ in _DAEMON_TRIGGERS:
        if store.get_state(_last_fired_key(name)) is not None:
            continue  # already primed from a previous run
        archive_fire = _most_recent_archive_fire(store, name)
        if archive_fire is not None and (
            (now - archive_fire.astimezone(tz)).total_seconds() < 7 * 86400
        ):
            store.set_state(_last_fired_key(name), archive_fire.isoformat())
            print(f"[primed] {name}: backfill from archive = {archive_fire.isoformat()}")
        else:
            store.set_state(_last_fired_key(name), now.isoformat())
            print(f"[primed] {name}: first-run baseline = {now.isoformat()}")

    # Show the upcoming fire times once at startup so the log is readable.
    for name, t in _compute_next_fires(now, config):
        delta = t - now
        print(f"upcoming {name}: {t.strftime('%Y-%m-%d %H:%M %Z')} (in {delta})")

    while True:
        now = datetime.now(tz)
        sched_cfg = config.get("scheduled", {})

        for name, last_scheduled_fn, cfg_key in _DAEMON_TRIGGERS:
            cfg = sched_cfg.get(cfg_key, {})
            last_scheduled = last_scheduled_fn(cfg, now)
            if last_scheduled is None:
                continue
            last_fired_str = store.get_state(_last_fired_key(name))
            if not last_fired_str:
                continue  # primed above; shouldn't happen
            last_fired = datetime.fromisoformat(last_fired_str)
            if last_fired >= last_scheduled:
                continue  # already fired since last scheduled time

            # Missed fire — catch up.
            delay = (now - last_scheduled).total_seconds()
            print(f"[catchup] {name} missed by {delay:.0f}s — firing now")
            try:
                await _fire_trigger(name)
                store.set_state(_last_fired_key(name), datetime.now(tz).isoformat())
            except Exception as e:  # noqa: BLE001
                print(f"[fire error] {name}: {e}", file=sys.stderr)
                # Don't update last_fired on error — try again next tick.

        # Fire any one-off reminders the agent has scheduled. Independent
        # of the static morning_brief / weekly_review checks above.
        try:
            _fire_due_reminders(store)
        except Exception as e:  # noqa: BLE001
            print(f"[reminders error] {e}", file=sys.stderr)

        # Email watch — gated by `email_triggers.enabled` and throttled
        # internally to every_minutes (default 15). Runs on every tick
        # but the throttle inside _fire_email_watch keeps it from
        # actually hitting Gmail more than once per window.
        try:
            _fire_email_watch(store, config, now)
        except Exception as e:  # noqa: BLE001
            print(f"[email_watch error] {e}", file=sys.stderr)

        # Re-read config so triggers.yaml edits take effect within ~30s.
        config = _load_config()
        await asyncio.sleep(TICK_SECONDS)


# ─── Diagnostics ─────────────────────────────────────────────────────────────


def _check() -> int:
    print("=== scheduler diagnostics ===\n")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY not set")
        return 1
    print("✓ ANTHROPIC_API_KEY set")

    from relay.sender import current_transport

    transport = current_transport()
    print(f"✓ transport: {transport}")
    try:
        sender = make_sender()
        # Probe the destination identifier without actually sending.
        dest = getattr(sender, "target_handle", None) or getattr(sender, "chat_id", None)
        print(f"✓ destination: {dest}")
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1

    try:
        config = _load_config()
        print(f"✓ config loaded from {CONFIG_PATH}")
    except FileNotFoundError as e:
        print(f"✗ {e}")
        return 1

    tz = _user_tz()
    now_local = datetime.now(tz)
    fires = _compute_next_fires(now_local, config)
    if not fires:
        print("(no triggers enabled in triggers.yaml)")
    else:
        print(f"\nupcoming fires (tz={tz.zone}):")
        for name, t in fires:
            delta = t - now_local
            print(f"  {name}: {t.strftime('%Y-%m-%d %H:%M %Z')} (in {delta})")
    return 0


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(_check())

    if "--run-now" in sys.argv:
        idx = sys.argv.index("--run-now")
        if idx + 1 >= len(sys.argv):
            print("usage: --run-now <morning_brief|weekly_review>", file=sys.stderr)
            sys.exit(2)
        trigger = sys.argv[idx + 1]
        if trigger not in PROMPTS:
            print(f"unknown trigger: {trigger}. valid: {list(PROMPTS)}", file=sys.stderr)
            sys.exit(2)
        try:
            asyncio.run(_fire_trigger(trigger))
        except KeyboardInterrupt:
            print("\nfire cancelled.")
        return

    try:
        asyncio.run(_run_daemon())
    except KeyboardInterrupt:
        print("\nscheduler stopped.")


if __name__ == "__main__":
    main()
