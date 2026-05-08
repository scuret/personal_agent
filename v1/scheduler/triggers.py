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
import os
import sys
from datetime import datetime, time as dtime, timedelta
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
from relay.imessage_relay import (  # noqa: E402
    MODE_CONTACT,
    MODE_SELF,
    ChatSender,
    _self_handles,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "triggers.yaml"
CONVERSATION_SOURCE = "scheduler"

# Synthetic prompts keyed by trigger name. The agent's personality + tools
# do the actual work — these prompts just frame the request.
PROMPTS: dict[str, str] = {
    "morning_brief": (
        "Good morning — generate the principal's daily brief, sent unprompted "
        "(they did not ask for this; you're proactively waking them up to it). "
        "Cover three things, in this order:\n"
        "  1. today's calendar — call calendar_list_events for today only.\n"
        "  2. top tasks — call todoist_list_tasks with filter 'today | overdue'.\n"
        "  3. urgent unread email — call gmail_search 'is:unread newer_than:1d'. "
        "     Skim the senders/subjects; flag only the ones that actually look "
        "     important (not newsletters, receipts, automated notifications).\n"
        "Then write 1-3 bubbles total in your normal voice — terse, lowercase, "
        "specific. No 'good morning!' preamble. Don't list every task or every "
        "email — curate. The principal is reading this on their phone."
    ),
    "weekly_review": (
        "It's Sunday evening — generate the principal's weekly review, sent "
        "unprompted. Cover:\n"
        "  1. last week's incomplete tasks — call todoist_list_tasks with "
        "     filter 'overdue'. Be honest about what slipped.\n"
        "  2. the upcoming week's calendar — call calendar_list_events for "
        "     the next 7 days. Highlight anything heavy or worth prepping for.\n"
        "Write 3-5 bubbles. Set up the week, don't recap exhaustively. "
        "If nothing slipped and the week looks light, say so."
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
    if not cfg.get("enabled", False):
        return None
    fire_time = _parse_hhmm(cfg.get("time", "20:00"))
    target_weekday = _DAY_NAME_TO_WEEKDAY.get(cfg.get("day", "sunday").lower(), 6)

    # Days from today's weekday to the target weekday (1..7 forward).
    delta_days = (target_weekday - now_local.weekday()) % 7
    candidate = (now_local + timedelta(days=delta_days)).replace(
        hour=fire_time.hour, minute=fire_time.minute, second=0, microsecond=0
    )
    if candidate <= now_local:
        candidate += timedelta(days=7)
    return candidate


def _compute_next_fires(now_local: datetime, config: dict[str, Any]) -> list[tuple[str, datetime]]:
    fires: list[tuple[str, datetime]] = []
    sched = config.get("scheduled", {})
    if (mb := _next_morning_brief_fire(sched.get("morning_brief", {}), now_local)) is not None:
        fires.append(("morning_brief", mb))
    if (wr := _next_weekly_review_fire(sched.get("weekly_review", {}), now_local)) is not None:
        fires.append(("weekly_review", wr))
    fires.sort(key=lambda x: x[1])
    return fires


# ─── Sending ─────────────────────────────────────────────────────────────────


def _resolve_send_handle() -> str:
    """The destination for scheduler iMessages — same handle the relay uses."""
    mode = os.environ.get("IMESSAGE_MODE", MODE_CONTACT).strip().lower()
    if mode == MODE_SELF:
        handles = _self_handles()
        if not handles:
            raise RuntimeError("IMESSAGE_MODE=self but no TARGET_PHONE_NUMBER set")
        return handles[0]
    target = os.environ.get("TARGET_PHONE_NUMBER", "").strip()
    if not target:
        raise RuntimeError("TARGET_PHONE_NUMBER not set")
    return target


async def _fire_trigger(trigger_name: str) -> None:
    """Generate the brief and send it via iMessage. One conversation per fire."""
    prompt = PROMPTS.get(trigger_name)
    if not prompt:
        print(f"[fire] unknown trigger: {trigger_name}", file=sys.stderr)
        return

    store = MemoryStore()
    sender = ChatSender(_resolve_send_handle())
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


async def _run_daemon() -> None:
    config = _load_config()
    tz = _user_tz()

    print(f"scheduler started (tz={tz.zone}). ctrl-c to stop.")

    while True:
        now_local = datetime.now(tz)
        fires = _compute_next_fires(now_local, config)
        if not fires:
            # Nothing enabled — sleep an hour and re-check (in case the
            # config gets edited and we re-read it on the next pass).
            print("[scheduler] no enabled triggers; sleeping 1h")
            await asyncio.sleep(3600)
            config = _load_config()
            continue

        next_name, next_time = fires[0]
        wait_seconds = (next_time - now_local).total_seconds()
        wait_seconds = max(wait_seconds, 1.0)  # never busy-loop
        print(
            f"next fire: {next_name} at {next_time.strftime('%Y-%m-%d %H:%M %Z')} "
            f"(in {wait_seconds:.0f}s)"
        )
        await asyncio.sleep(wait_seconds)

        try:
            await _fire_trigger(next_name)
        except Exception as e:  # noqa: BLE001 — never let one bad fire kill the daemon
            print(f"[fire error] {e}", file=sys.stderr)

        # Re-read config after each fire so edits to triggers.yaml take
        # effect on the next cycle without restarting the daemon.
        config = _load_config()


# ─── Diagnostics ─────────────────────────────────────────────────────────────


def _check() -> int:
    print("=== scheduler diagnostics ===\n")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY not set")
        return 1
    print("✓ ANTHROPIC_API_KEY set")

    try:
        send_handle = _resolve_send_handle()
        print(f"✓ destination handle: {send_handle}")
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
