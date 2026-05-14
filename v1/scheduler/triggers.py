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
import base64
import json
import os
import re
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytz
import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

# Model used for trigger fires (morning_brief, weekly_review).
# Stronger than the relay's default (Sonnet) because:
#   • brief prompts ask for tight format with priority-ordering rules
#     that smaller models confabulate to satisfy
#   • brief output is unprompted — the principal can't easily ask
#     follow-ups to correct a bad brief
# Cost overhead is ~2× per fire, but only a few fires per week.
TRIGGER_MODEL = "claude-opus-4-7"

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

# Synthetic prompts keyed by trigger name. Todoist data is pre-rendered
# in Python (see `_render_todoist_block`) and injected after the prompt
# body — the agent surfaces tasks FROM that injected block verbatim
# instead of calling todoist_list_tasks itself and risking confabulation.
PROMPTS: dict[str, str] = {
    "morning_brief": (
        "Generate the principal's daily brief, sent unprompted. The "
        "voice is a sharp peer making sense of the day — not a status "
        "report. Conversational, lowercase, no formatting (iMessage "
        "doesn't render markdown).\n"
        "\n"
        "Pull these yourself, in parallel:\n"
        "  • calendar_list_events for today only\n"
        "  • gmail_search 'is:unread newer_than:2d' — identify emails "
        "that genuinely need the principal's action (response, decision, "
        "follow-up). Skip newsletters, receipts, automated alerts, "
        "marketing, social notifications. For each actionable email, "
        "note if it's been sitting many days unread.\n"
        "  • weather_current for the principal's location — used to "
        "inform task suggestions (good day for outdoor stuff, rain "
        "coming, etc.)\n"
        "\n"
        "TODOIST DATA: do NOT call todoist_list_tasks for brief content. "
        "An authoritative pre-rendered Todoist block is appended below "
        "with three things: a progress note (overdue P1 cleared since "
        "last brief, if any), the full Overdue P1 list, and the full "
        "Today P1 list. Ignore the P2/P3/P4 sections and the "
        "lower-priority count — the brief is P1 only.\n"
        "\n"
        "DELIVERIES DATA: if a DELIVERIES TODAY block is appended below, "
        "surface those packages in a \"📦 deliveries today:\" section. "
        "Quote the subject verbatim, include the tracking number "
        "inline, and put the tracking URL on its own line so iMessage "
        "auto-links it as a tap target. Do NOT call gmail_search for "
        "deliveries — the block is the source of truth. Omit the "
        "section entirely if no deliveries block was appended.\n"
        "\n"
        "SLEEP DATA: if a LAST NIGHT'S SLEEP block is appended below, "
        "lead the brief with a short conversational line acknowledging "
        "the sleep score + total time. e.g. \"slept 6h 42m — score 78\" "
        "or \"6 hours, HRV's lower than usual\". Don't enumerate every "
        "metric in the block; pick the 1-2 most informative for the "
        "principal's day. Omit if no sleep block.\n"
        "\n"
        "Names from the Todoist block may be lightly paraphrased for "
        "brevity (\"send state farm insurance inventory and bills of "
        "sale\" → \"state farm docs\"; \"Put Grayson's braces on\" → "
        "\"grayson braces\"). But every task you surface must "
        "correspond to a real entry in the block — no inventing items, "
        "due dates, or counts.\n"
        "\n"
        "Structure (in this order — OMIT any section that has nothing "
        "real to surface; do not write 'nothing to report' or "
        "equivalents):\n"
        "\n"
        "1. Progress opener (only if the block notes overdue P1 cleared "
        "since last brief): one short conversational line acknowledging "
        "it. e.g. \"overdue cleared from 4 to 2, nice\" or \"todo "
        "digest — knocked out 3 P1s since yesterday.\" Skip if no "
        "progress to note or if this is the first brief.\n"
        "\n"
        "2. \"today's big ones:\" — every overdue P1 and every today P1 "
        "from the block, one per dash-line. Inline context tags where "
        "relevant: time-of-day if the task or a calendar event has one "
        "(\"grayson braces 6:30pm\"), a weather tie-in for outdoor "
        "tasks (\"sand porch trim — weather's good\"), or a "
        "days-overdue note. Combine related tasks on one line if "
        "natural (\"state farm docs + local agent move\"). Keep each "
        "line tight.\n"
        "\n"
        "3. Weather line — only if it's notably good/bad or affects "
        "task choice. One conversational line woven into productivity "
        "context. e.g. \"sunny 77 today — perfect for porch work or "
        "annie gunn's curbside if you haven't done anniversary yet.\" "
        "Skip if the weather is unremarkable.\n"
        "\n"
        "3a. \"📦 deliveries today:\" — only if a DELIVERIES DATA block "
        "was appended below. One dash-line per package: carrier + "
        "subject + tracking number, with the tracking URL on its own "
        "indented line directly below so iMessage renders it as a "
        "tappable link. Omit this section entirely if no deliveries "
        "block is present.\n"
        "\n"
        "4. \"couple adds to the list:\" — items from email/calendar "
        "that look like new action items. Phrase as suggested additions "
        "in lowercase imperative (\"call brooke + yorek\", \"schedule "
        "raj next wed\"). For long-waiting items, note the wait "
        "(\"jessica benson follow-up — 15 days waiting\"). Cap at 5. "
        "Do NOT actually create Todoist tasks — these are surfaced as "
        "suggestions; the principal decides whether to add them.\n"
        "\n"
        "Output budget: target ~600 chars, hard cap ~1200. No "
        "preamble. No closing summary. Brief ends after the last "
        "non-empty section."
    ),
    "weekly_review": (
        "Generate the principal's Sunday-evening weekly review, sent "
        "unprompted. Voice: sharp peer reflecting on the week — "
        "conversational, lowercase, no formatting (iMessage doesn't "
        "render markdown).\n"
        "\n"
        "Pull this yourself:\n"
        "  • calendar_list_events for the next 7 days — heavy/important "
        "days only\n"
        "\n"
        "TODOIST DATA: do NOT call todoist_list_tasks. An authoritative "
        "pre-rendered Todoist block is appended below. Lead with P1/P2 "
        "overdue items from that block. Names may be lightly paraphrased "
        "for brevity (\"send state farm insurance inventory and bills "
        "of sale\" → \"state farm docs\"), but every item must "
        "correspond 1:1 to a real entry in the block — no inventing. "
        "Lower-priority overdue is already aggregated as a count — use "
        "that count, don't invent specifics. If a category is empty, "
        "omit it.\n"
        "\n"
        "Structure (lowercase prose openers, omit empty sections):\n"
        "  \"what slipped this week:\" — overdue P1/P2 list\n"
        "  \"week ahead:\" — heavy/important days only, don't list "
        "every event\n"
        "\n"
        "Output budget: target ~400 chars, hard cap ~700. No preamble. "
        "No closing summary. Honest but brief about slippage."
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
_DELIVERY_WATCH_LAST_CHECK_KEY = "delivery_watch_last_check"
_DELIVERY_WATCH_SEEN_KEY = "delivery_watch_seen_ids"
# Delivery state cap — same shape as the email-watch seen-set, just
# kept independent so a delivery alert doesn't displace a regular
# urgency-watch entry.
_DELIVERY_WATCH_SEEN_CAP = 200

# Default carrier sender substrings. Used when triggers.yaml doesn't
# override; the substrings match the From: header case-insensitively.
# Add or override via `delivery_watch.senders` in config.
_DELIVERY_DEFAULT_SENDERS = [
    "ups.com",                          # UPS
    "fedex.com",                        # FedEx
    "amazon.com",                       # Amazon shipment-tracking@
    "usps.com",                         # USPS
    "informeddelivery.usps.com",        # USPS Informed Delivery (morning mail preview)
    "dhl.com",
    "dhlexpress.com",
]

# Default keywords (subject OR snippet, case-insensitive) that indicate
# a package is scheduled to arrive today or has just arrived. The
# delivery_watch path requires BOTH a sender hit AND a keyword hit —
# stricter than email_watch's OR logic so we don't fire for every
# "your account was updated" carrier email.
_DELIVERY_DEFAULT_KEYWORDS = [
    "out for delivery",
    "delivering today",
    "scheduled to deliver today",
    "arriving today",
    "your delivery today",
    "delivery today",
    "delivered",
]


def _carrier_label(sender: str) -> str:
    """Short carrier name from a From: header. Used in the alert text."""
    s = (sender or "").lower()
    if "ups.com" in s:
        return "UPS"
    if "fedex.com" in s:
        return "FedEx"
    if "amazon" in s:
        return "Amazon"
    if "usps" in s or "informeddelivery" in s:
        return "USPS"
    if "dhl" in s:
        return "DHL"
    return "Carrier"


def _delivery_email_matches(
    email: dict[str, Any],
    senders: list[str],
    keywords: list[str],
) -> tuple[bool, str]:
    """AND match: sender substring AND keyword in subject/snippet.

    Returns (matched, reason). The stricter logic vs email_watch keeps
    routine "your account" emails from carriers out of the alert path.
    """
    sender_str = (email.get("from") or "").lower()
    sender_hit = next((s for s in senders if s.lower() in sender_str), None)
    if not sender_hit:
        return False, ""

    haystack = (
        (email.get("subject") or "") + " " + (email.get("snippet") or "")
    ).lower()
    keyword_hit = next((k for k in keywords if k.lower() in haystack), None)
    if not keyword_hit:
        return False, ""

    return True, f"{sender_hit} | '{keyword_hit}'"
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


def _short_sender(from_header: str) -> str:
    """Pull a friendly sender name from a 'Display Name <addr@example.com>' header."""
    s = (from_header or "").strip()
    if "<" in s:
        s = s.split("<")[0].strip().strip('"')
    if len(s) > 40:
        s = s[:40] + "…"
    return s or "?"


# Haiku model used for the email-watch helpers (summarizer + classifier).
# Cheaper + faster than Sonnet; both tasks are single-shot text-in/text-
# out where Haiku quality matches Sonnet. Override via SUMMARIZER_MODEL.
_SUMMARIZER_MODEL = os.environ.get("SUMMARIZER_MODEL", "claude-haiku-4-5-20251001")
_SUMMARIZER_MAX_BODY_CHARS = 4000  # cap input to keep Haiku call bounded
_SUMMARIZER_MAX_TOKENS = 200

# Cost-control cap for the LLM classifier (per fire). The classifier
# only runs on emails that DIDN'T match the rules-based filter, and
# the seen-id dedup means each email is classified at most once, so
# this cap is mostly defensive against pathological bulk-mail days.
_LLM_CLASSIFIER_MAX_PER_CHECK = 30
_LLM_CLASSIFIER_BODY_CHARS = 1500  # tighter than summarizer — 1 word out

# Cheap pre-filter: substrings in the From header that almost always
# mean "automated / never worth a personal response." Saves a Haiku
# call per match on a bulky inbox day.
_AUTOMATED_SENDER_PATTERNS = (
    "no-reply",
    "noreply",
    "donotreply",
    "do-not-reply",
    "notifications@",
    "notification@",
    "newsletter@",
    "marketing@",
    "promotions@",
    "promo@",
    "alerts@",
    "mailer-daemon",
    "bounce@",
    "@em.",
    "@mailer.",
    "@notify.",
)


def _is_automated_sender(from_header: str) -> bool:
    """Heuristic: From header looks like a bulk-mailer / automated source."""
    s = (from_header or "").lower()
    return any(pat in s for pat in _AUTOMATED_SENDER_PATTERNS)


def _triage_email_with_haiku(
    email: dict[str, Any],
    today_local: datetime,
    tz_name: str,
) -> dict[str, Any]:
    """Single Haiku call that BOTH decides "is this worth a phone ping?"
    AND produces structured ping items when the answer is yes.

    Returns:
        {"alert": bool, "items": list[str]}

    Each item is a complete, action-shaped blurb (1-3 sentences) that
    becomes one iMessage / Telegram bubble. A single email may produce
    multiple items when it covers multiple distinct events (a school
    newsletter with both a field trip and Field Day → two items).

    On any failure (body fetch, Haiku error, output unparseable) returns
    {"alert": False, "items": []} — email-watch is a pure notification
    path and must never crash the daemon.
    """
    try:
        body = _fetch_email_body(email["id"])
    except Exception as e:  # noqa: BLE001
        print(f"[email_watch] body fetch failed: {e}", file=sys.stderr)
        body = ""
    if not body:
        body = email.get("snippet") or ""
    if not body.strip():
        return {"alert": False, "items": []}

    body_capped = body[:_SUMMARIZER_MAX_BODY_CHARS]
    today_long = today_local.strftime("%A, %B %d, %Y")
    today_iso = today_local.strftime("%Y-%m-%d")

    prompt = (
        "You are an inbox triage agent for one user. Output goes to "
        "their phone as iMessage bubbles, so be concise and action-shaped.\n"
        "\n"
        f"Today is {today_long} ({today_iso}). Timezone: {tz_name}.\n"
        "\n"
        "Read the email below. Two questions:\n"
        "\n"
        "(1) Is this worth pinging the user's phone? Bias toward NO. "
        "Flag YES only for:\n"
        "  - Real-person requests, decisions, RSVPs, or replies-needed\n"
        "  - Logistics for upcoming events the user (or their family) "
        "will attend: date, time, what-to-bring, what-to-decide, "
        "drop-off / pick-up details\n"
        "  - Deadlines and time-sensitive asks\n"
        "  - School emails about their kids (events, schedules, "
        "decisions, things to send in)\n"
        "  - Meetings missing expected prep materials\n"
        "Always NO for: newsletters / digests / marketing / receipts / "
        "automated notifications / FYI with no ask / anything the user "
        "could safely ignore for a week.\n"
        "\n"
        "(2) If YES: extract ONE OR MORE 'ping items'. An email about "
        "two events = two items. An email about one event = one item. "
        "Each item is self-contained — give the user everything they "
        "need without opening the email.\n"
        "\n"
        "Item style:\n"
        "  - Lead with a date/time hook. Use relative phrasing when "
        "within 7 days: 'Tomorrow (Thurs 5/14) - ...', 'Today at 3pm - "
        "...', 'Friday (5/15) - ...'. Beyond 7 days: 'Monday May 18 - "
        "...'.\n"
        "  - Include location, time, what to bring, decision needed, "
        "or who to contact — whichever apply.\n"
        "  - Plain text. No markdown. No 'see attached.' Use '&' over "
        "'and' where it shortens.\n"
        "  - 280 chars max per item.\n"
        "  - Match the voice of: 'Tomorrow (Thurs 5/14) - Queeny Park "
        "hike. Arrive 9:15 AM with bag lunch & proper shoes. Booster "
        "seat drop-off Thurs AM if needed'.\n"
        "\n"
        "Output format — strict:\n"
        "  ALERT: no\n"
        "OR\n"
        "  ALERT: yes\n"
        "  ITEM:\n"
        "  <ping text>\n"
        "  ITEM:\n"
        "  <ping text>\n"
        "\n"
        "Do not add preamble, postamble, or any other content outside "
        "this format.\n"
        "\n"
        "--- EMAIL ---\n"
        f"From: {email.get('from', '')}\n"
        f"Subject: {email.get('subject', '') or '(no subject)'}\n"
        f"Date: {email.get('date', '')}\n"
        "\n"
        f"{body_capped}"
    )

    try:
        import anthropic  # late import — keep daemon-startup lean

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_SUMMARIZER_MODEL,
            max_tokens=_SUMMARIZER_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        out = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
    except Exception as e:  # noqa: BLE001
        print(f"[email_watch] triage call failed: {e}", file=sys.stderr)
        return {"alert": False, "items": []}

    return _parse_triage_output(out)


def _parse_triage_output(out: str) -> dict[str, Any]:
    """Parse the ALERT:/ITEM: protocol used by _triage_email_with_haiku.

    Tolerant of stray whitespace and mixed casing. Malformed output or
    an explicit `ALERT: no` both return {"alert": False, "items": []}.
    """
    alert = False
    items: list[str] = []
    in_item = False
    current: list[str] = []
    for raw in (out or "").splitlines():
        s = raw.strip()
        upper = s.upper()
        if upper.startswith("ALERT:"):
            decision = upper.split(":", 1)[1].strip()
            alert = decision.startswith("YES")
            in_item = False
            current = []
            continue
        if upper == "ITEM:":
            if in_item and current:
                items.append("\n".join(current).strip())
            in_item = True
            current = []
            continue
        if in_item:
            current.append(raw)
    if in_item and current:
        items.append("\n".join(current).strip())

    items = [it for it in items if it]
    if not alert:
        return {"alert": False, "items": []}
    return {"alert": True, "items": items}


def _format_email_alert_items(items: list[str]) -> str:
    """Combine ping items into one text body for a single transport send.

    Items get separated by a blank line so iMessage / Telegram still
    visually segment them. Used when the sender path delivers a single
    payload; the multi-bubble UX comes from sending each item back-to-
    back via `_fire_email_watch` instead.
    """
    return "\n\n".join(it.strip() for it in items if it.strip())


def _fire_email_watch(store: MemoryStore, config: dict[str, Any], now: datetime) -> None:
    cfg = (config.get("email_triggers") or {})
    if not cfg.get("enabled"):
        return

    # Per-user privacy opt-out. When true, no email content is sent to
    # Anthropic for triage — and that means no email pings either,
    # since the triage IS what writes the pings. Other scheduler
    # surfaces (morning brief, delivery watch, expected arrivals) are
    # unaffected. See ROADMAP "Security enhancements" M4.
    if os.environ.get("EMAIL_TRIAGE_LOCAL_ONLY", "").strip().lower() in {"1", "true", "yes"}:
        store.set_state(_EMAIL_WATCH_LAST_CHECK_KEY, now.isoformat())
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

    # Load the seen-id set from state. Each id is triaged at most once
    # per its lifetime in the set.
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

    # LLM-only triage: every non-automated unread email gets one Haiku
    # call that BOTH decides "ping the user?" AND produces structured
    # ping items. The previous rules-based allowlist + urgency-keyword
    # gate is gone — Haiku judges every candidate in context, which
    # surfaces things like school logistics emails from senders the
    # user hasn't explicitly named. Cost is bounded by max_per_check
    # and the automated-sender pre-filter (no Haiku call for bulk-
    # mailer From: headers).
    triage_cfg = (cfg.get("llm_classification") or {})
    max_per_check = int(triage_cfg.get("max_per_check", _LLM_CLASSIFIER_MAX_PER_CHECK))

    triaged: list[tuple[dict[str, Any], list[str]]] = []
    classified = 0
    skipped_automated = 0
    today_local = now  # `now` arrives in user-local tz already
    tz_name = str(now.tzinfo) if now.tzinfo else "UTC"
    for email in emails:
        eid = email["id"]
        if eid in seen:
            continue
        seen.add(eid)
        if _is_automated_sender(email.get("from", "")):
            skipped_automated += 1
            continue
        if classified >= max_per_check:
            # Out of budget for this fire — leave the id un-seen so the
            # next fire picks it up. We added it to `seen` above, so
            # remove it back out to preserve that property.
            seen.discard(eid)
            continue
        classified += 1
        result = _triage_email_with_haiku(email, today_local, tz_name)
        if result.get("alert") and result.get("items"):
            triaged.append((email, result["items"]))

    # Cap and persist seen set.
    if len(seen) > _EMAIL_WATCH_SEEN_CAP:
        seen = set(list(seen)[-_EMAIL_WATCH_SEEN_CAP:])
    store.set_state(_EMAIL_WATCH_SEEN_KEY, json.dumps(list(seen)))
    store.set_state(_EMAIL_WATCH_LAST_CHECK_KEY, now.isoformat())

    if classified:
        print(
            f"[email_watch] triaged {classified} email(s), "
            f"flagged {len(triaged)}, skipped {skipped_automated} automated"
        )
        # Visibility into the Anthropic data flow. Logged whether or not
        # the brief later surfaces it. ROADMAP M4.
        try:
            store.log_api_event(
                kind="email_triage_run",
                payload={"classified": classified, "flagged": len(triaged)},
            )
        except Exception as e:  # noqa: BLE001 — bookkeeping shouldn't crash
            print(f"[email_watch] triage-count log failed: {e}", file=sys.stderr)

    if not triaged:
        return

    # Send each ping item as its own transport message so the user sees
    # them as separate iMessage / Telegram bubbles — that's the visual
    # shape the third-party reference agent uses and the action-shaped
    # blurbs read much better when split.
    sender = make_sender()
    total_items = sum(len(items) for _, items in triaged)
    sent_items = 0
    for _email, items in triaged:
        for body in items:
            ok, err = sender.send(body)
            if ok:
                sent_items += 1
            else:
                print(f"[email_watch] send failed: {err}", file=sys.stderr)

    if sent_items:
        print(
            f"[email_watch] notified — {sent_items}/{total_items} item(s) "
            f"from {len(triaged)} email(s)"
        )
        # Stash each alerted email as a fact so the agent has a referent
        # when the principal replies "draft a response" in a separate
        # session — email-watch is a pure-Python notification with no
        # LLM turn, so this is the only handoff into agent-visible state.
        alerted_at = now.isoformat()
        for email, _items in triaged:
            try:
                store.log_fact(
                    content=(
                        f"message_id={email['id']} "
                        f"thread_id={email.get('thread_id', '')} "
                        f"from={email.get('from', '')} "
                        f"subject={email.get('subject', '') or '(no subject)'} "
                        f"reason=llm-triage "
                        f"alerted_at={alerted_at}"
                    ),
                    category="alerted_email",
                )
            except Exception as e:  # noqa: BLE001 — bookkeeping shouldn't crash the daemon
                print(f"[email_watch] log_fact failed: {e}", file=sys.stderr)


# ─── Tracking extraction ───────────────────────────────────────────────────
#
# For each delivery email we match, fetch the full body and pull out the
# tracking number using carrier-specific patterns. The alert + the logged
# fact include both the raw tracking number and a clickable tracking URL
# (iMessage auto-links plaintext URLs).
#
# Patterns are tuned per carrier — UPS's 1Z prefix is highly specific;
# FedEx is loose (12-15 digits) so we anchor it via the carrier context;
# USPS uses several 20-22 digit formats. Amazon doesn't expose a public
# tracking number for last-mile deliveries (TBA prefix is for their own
# logistics) — we extract the order ID from the body as a fallback.

_TRACKING_PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    "UPS": (
        re.compile(r"\b1Z[0-9A-Z]{16}\b"),
        "https://www.ups.com/track?tracknum={t}",
    ),
    "FedEx": (
        re.compile(r"\b\d{12,15}\b"),
        "https://www.fedex.com/fedextrack/?trknbr={t}",
    ),
    "USPS": (
        re.compile(
            r"\b(?:9[24]\d{18,20}|EA\d{9}US|LK\d{9}US|"
            r"94001\d{17}|92\d{18,20}|420\d{27})\b"
        ),
        "https://tools.usps.com/go/TrackConfirmAction?tLabels={t}",
    ),
    "DHL": (
        re.compile(r"\b\d{10,11}\b"),
        "https://www.dhl.com/us-en/home/tracking.html?tracking-id={t}",
    ),
    "Amazon": (
        # Amazon's last-mile carrier prefix. Most Amazon emails just link
        # to the order page; this catches TBA numbers when present.
        re.compile(r"\bTBA\d{12}\b"),
        "https://www.amazon.com/gp/your-account/order-history",
    ),
}

# Amazon fallback: order ID in URL query params. Pattern matches both
# orderID=XXX-NNNNNNN-NNNNNNN (digit suffix may vary).
_AMAZON_ORDER_ID_PATTERN = re.compile(r"orderID=([0-9A-Z\-]+)")


def _fetch_email_body(message_id: str) -> str:
    """Pull the full text body of a Gmail message.

    Walks the MIME payload tree, preferring text/plain. Falls back to
    text/html with a naive tag strip. Returns "" on any error so callers
    can degrade gracefully (alert still goes out without tracking info).
    """
    try:
        from mcp_servers.google_auth import build_service  # lazy import

        svc = build_service("gmail", "v1")
        msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    except Exception as e:  # noqa: BLE001
        print(f"[delivery_watch] body fetch failed for {message_id}: {e}", file=sys.stderr)
        return ""

    def find(part: dict[str, Any], mime_prefix: str) -> str | None:
        if (part.get("mimeType") or "").startswith(mime_prefix):
            data = (part.get("body") or {}).get("data") or ""
            if data:
                try:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except (ValueError, UnicodeDecodeError):
                    return None
        for sub in part.get("parts") or []:
            found = find(sub, mime_prefix)
            if found:
                return found
        return None

    payload = msg.get("payload") or {}
    text = find(payload, "text/plain")
    if not text:
        html = find(payload, "text/html")
        if html:
            text = re.sub(r"<[^>]+>", " ", html)
    return text or ""


def _extract_tracking(carrier: str, body: str) -> tuple[str | None, str | None]:
    """Return (tracking_number, tracking_url) for a delivery email.

    The body is the full plain-text email content (post-MIME-walk). For
    each carrier we run its specific pattern; first match wins. Amazon
    has a fallback path that extracts the order ID from a URL query
    param when no TBA tracking number is present.
    """
    pattern_info = _TRACKING_PATTERNS.get(carrier)
    if pattern_info:
        pattern, url_template = pattern_info
        match = pattern.search(body)
        if match:
            tracking = match.group(0)
            url = url_template.format(t=tracking)
            return tracking, url

    if carrier == "Amazon":
        m = _AMAZON_ORDER_ID_PATTERN.search(body)
        if m:
            order_id = m.group(1)
            return (
                f"order {order_id}",
                f"https://www.amazon.com/gp/your-account/order-details?orderID={order_id}",
            )

    return None, None


def _fire_delivery_watch(store: MemoryStore, config: dict[str, Any], now: datetime) -> None:
    """Poll Gmail for carrier emails announcing a delivery today.

    Parallel to email_watch but stricter: requires BOTH a known-carrier
    sender AND a delivery-today keyword in the subject or snippet. Sends
    a "📦" alert distinct from email_watch's "📧" so the principal can
    tell at a glance what kind of ping just landed.

    Also logs each alerted delivery as a fact (`category='delivery_today'`)
    so the morning brief can pull a "deliveries today" rollup without
    re-querying Gmail.
    """
    cfg = (config.get("delivery_watch") or {})
    if not cfg.get("enabled"):
        return

    every = int(cfg.get("every_minutes", 30))
    last_check_str = store.get_state(_DELIVERY_WATCH_LAST_CHECK_KEY)
    if last_check_str:
        try:
            last_check = datetime.fromisoformat(last_check_str)
        except ValueError:
            last_check = None
        if last_check is not None and (now - last_check).total_seconds() < every * 60:
            return  # throttled

    senders = list(cfg.get("senders") or _DELIVERY_DEFAULT_SENDERS)
    keywords = list(cfg.get("keywords") or _DELIVERY_DEFAULT_KEYWORDS)

    # Seen-set dedup keyed on Gmail message ID — survives daemon restarts
    # via the state KV.
    try:
        seen_list = json.loads(store.get_state(_DELIVERY_WATCH_SEEN_KEY, "[]") or "[]")
        if not isinstance(seen_list, list):
            seen_list = []
    except json.JSONDecodeError:
        seen_list = []
    seen: set[str] = set(seen_list)

    try:
        emails = _fetch_recent_unread_gmail(limit=50)
    except Exception as e:  # noqa: BLE001
        print(f"[delivery_watch] gmail fetch failed: {e}", file=sys.stderr)
        return

    flagged: list[tuple[dict[str, Any], str]] = []
    for email in emails:
        eid = email["id"]
        if eid in seen:
            continue
        seen.add(eid)
        matched, reason = _delivery_email_matches(email, senders, keywords)
        if matched:
            flagged.append((email, reason))

    if len(seen) > _DELIVERY_WATCH_SEEN_CAP:
        seen = set(list(seen)[-_DELIVERY_WATCH_SEEN_CAP:])
    store.set_state(_DELIVERY_WATCH_SEEN_KEY, json.dumps(list(seen)))
    store.set_state(_DELIVERY_WATCH_LAST_CHECK_KEY, now.isoformat())

    if not flagged:
        return

    # For each flagged email, fetch its body and extract a tracking number
    # + URL. Done once here so both the alert and the logged fact share
    # the same tracking data. Failures don't block the alert path.
    enriched: list[dict[str, Any]] = []
    for email, reason in flagged:
        carrier = _carrier_label(email["from"])
        body = _fetch_email_body(email["id"])
        # Search body + subject so we don't miss tracking numbers that
        # only appear in the subject line.
        search_text = body + " " + (email.get("subject") or "")
        tracking, url = _extract_tracking(carrier, search_text)
        enriched.append({
            "email": email,
            "reason": reason,
            "carrier": carrier,
            "tracking": tracking or "",
            "tracking_url": url or "",
        })

    if len(enriched) == 1:
        e = enriched[0]
        email = e["email"]
        subj = email.get("subject") or "(no subject)"
        text = f"📦 {e['carrier']} — {subj}"
        snippet = (email.get("snippet") or "").strip()
        if snippet:
            text += f"\n{snippet[:220]}"
        if e["tracking"]:
            text += f"\ntracking: {e['tracking']}"
        if e["tracking_url"]:
            text += f"\n{e['tracking_url']}"
    else:
        lines = [f"📦 {len(enriched)} delivery updates:"]
        for e in enriched:
            email = e["email"]
            subj = (email.get("subject") or "(no subject)")[:80]
            line = f"- {e['carrier']}: {subj}"
            if e["tracking"]:
                line += f"  ({e['tracking']})"
            lines.append(line)
            if e["tracking_url"]:
                lines.append(f"  {e['tracking_url']}")
        text = "\n".join(lines)

    sender = make_sender()
    ok, err = sender.send(text)
    if ok:
        print(f"[delivery_watch] notified — {len(enriched)} delivery email(s)")
        # Log each delivery as a fact so the morning brief can rollup
        # what's expected today without a redundant Gmail call.
        alerted_at = now.isoformat()
        for e in enriched:
            email = e["email"]
            try:
                store.log_fact(
                    content=(
                        f"carrier={e['carrier']} "
                        f"subject={email.get('subject') or '(no subject)'} "
                        f"tracking={e['tracking']} "
                        f"tracking_url={e['tracking_url']} "
                        f"message_id={email['id']} "
                        f"thread_id={email.get('thread_id', '')} "
                        f"from={email.get('from', '')} "
                        f"reason={e['reason']} "
                        f"alerted_at={alerted_at}"
                    ),
                    category="delivery_today",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[delivery_watch] log_fact failed: {exc}", file=sys.stderr)
    else:
        print(f"[delivery_watch] send failed: {err}", file=sys.stderr)


# ─── Expected arrivals (gap detection) ────────────────────────────────────
#
# For each watched event with a known sender + subject pattern + date,
# we periodically check whether the expected email has arrived. If it
# hasn't and we're inside the lead-time window, we ping the user. Each
# watch fires at most once per day so a stuck "Kara hasn't sent the
# packet yet" state doesn't notify you every 12 hours forever.

_EXPECTED_ARRIVALS_LAST_CHECK_KEY = "expected_arrivals_last_check"
_EXPECTED_ARRIVALS_LAST_PING_PREFIX = "expected_arrivals_last_ping:"


def _expected_arrival_already_received(
    expected_sender: str,
    expected_subject: str,
    since_dt: datetime,
) -> bool:
    """True if Gmail has any message matching the watch criteria since
    `since_dt`. Looks in inbox + archived (excludes spam/trash by default).

    On any error returns False — we'd rather false-positive ping the user
    than silently skip a real gap.
    """
    from mcp_servers.google_auth import build_service  # late import

    try:
        svc = build_service("gmail", "v1")
    except Exception as e:  # noqa: BLE001
        print(f"[expected_arrivals] gmail service init failed: {e}", file=sys.stderr)
        return False

    since_str = since_dt.strftime("%Y/%m/%d")
    parts = [f"after:{since_str}", f"from:{expected_sender}"]
    if expected_subject:
        # Gmail's `subject:` accepts an unquoted phrase; quote it so
        # spaces don't split into multiple AND-terms.
        parts.append(f'subject:"{expected_subject}"')
    query = " ".join(parts)
    try:
        resp = (
            svc.users()
            .messages()
            .list(userId="me", q=query, maxResults=1)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        print(f"[expected_arrivals] gmail query failed: {e}", file=sys.stderr)
        return False
    return bool(resp.get("messages"))


def _format_expected_arrival_alert(
    watch: dict[str, Any],
    event_dt: datetime,
    now_local: datetime,
) -> str:
    """Render the heads-up ping. Mirrors the third-party agent's voice:

      Heads up - the Fulton Board meeting is Monday May 18 and no
      prep materials have come in from Kara yet. Only 5 days out so
      keep an eye on your inbox.
    """
    name = (watch.get("name") or "expected email").strip()
    expected_sender = (watch.get("expected_sender") or "").strip()
    # `sender_label` is an optional config override for how the sender
    # gets named in the ping ("Chair" reads better than the bare email
    # address). Fall back to the email's local-part with sensible
    # title-casing when omitted.
    sender_short = (watch.get("sender_label") or "").strip()
    if not sender_short:
        sender_short = _short_sender(expected_sender) or ""
    if not sender_short or "@" in sender_short:
        local = expected_sender.split("@", 1)[0] if "@" in expected_sender else expected_sender
        sender_short = local.replace(".", " ").replace("_", " ").title() if local else "the sender"
    # Trim to first name when the label is "First Last".
    if " " in sender_short:
        sender_short = sender_short.split()[0]

    event_label = event_dt.strftime("%A %B %-d")
    days_out = (event_dt.date() - now_local.date()).days
    when_phrase = (
        "today" if days_out == 0
        else "tomorrow" if days_out == 1
        else f"in {days_out} days"
    )
    urgency = (
        f"only {days_out} days out" if 0 < days_out <= 7 else f"{days_out} days out"
    )

    return (
        f"Heads up - {name} is {event_label} ({when_phrase}) and no "
        f"email from {sender_short} yet. {urgency.capitalize()} so "
        f"keep an eye on your inbox."
    )


def _fire_expected_arrivals(
    store: MemoryStore, config: dict[str, Any], now: datetime
) -> None:
    """Check each configured watch; ping when the expected email is
    overdue and we're inside the lead-time window. Cadence-throttled
    via `expected_arrivals.cadence_hours` (default 12) and per-watch
    daily-throttled so the same gap doesn't re-notify constantly.
    """
    cfg = (config.get("expected_arrivals") or {})
    if not cfg.get("enabled"):
        return
    watches = cfg.get("watches") or []
    if not watches:
        return

    cadence_hours = float(cfg.get("cadence_hours", 12))
    last_check_str = store.get_state(_EXPECTED_ARRIVALS_LAST_CHECK_KEY)
    if last_check_str:
        try:
            last_check = datetime.fromisoformat(last_check_str)
        except ValueError:
            last_check = None
        if (
            last_check is not None
            and (now - last_check).total_seconds() < cadence_hours * 3600
        ):
            return
    store.set_state(_EXPECTED_ARRIVALS_LAST_CHECK_KEY, now.isoformat())

    tz = now.tzinfo
    today = now.date()
    for w in watches:
        name = w.get("name") or "expected arrival"
        event_date_str = (w.get("event_date") or "").strip()
        expected_sender = (w.get("expected_sender") or "").strip()
        expected_subject = (w.get("expected_subject") or "").strip()
        try:
            lead_time = int(w.get("lead_time_days", 7))
        except (TypeError, ValueError):
            lead_time = 7

        if not event_date_str or not expected_sender:
            continue

        try:
            event_date = datetime.fromisoformat(event_date_str).date()
        except ValueError:
            print(
                f"[expected_arrivals] {name}: bad event_date "
                f"{event_date_str!r}, skipping",
                file=sys.stderr,
            )
            continue

        # Outside the window in either direction.
        if today > event_date:
            continue  # event already passed
        if (event_date - today).days > lead_time:
            continue  # not yet inside lead time

        # Per-watch daily throttle.
        last_ping_key = (
            f"{_EXPECTED_ARRIVALS_LAST_PING_PREFIX}{name}|{event_date_str}"
        )
        last_ping_str = store.get_state(last_ping_key)
        if last_ping_str:
            try:
                last_ping = datetime.fromisoformat(last_ping_str)
                if (now - last_ping).total_seconds() < 24 * 3600:
                    continue
            except ValueError:
                pass

        # Search Gmail since the start of the lead-time window.
        since_dt = datetime.combine(
            event_date, dtime(0, 0), tzinfo=tz
        ) - timedelta(days=lead_time)
        if _expected_arrival_already_received(
            expected_sender, expected_subject, since_dt
        ):
            continue

        event_dt = datetime.combine(event_date, dtime(0, 0), tzinfo=tz)
        text = _format_expected_arrival_alert(w, event_dt, now)
        sender = make_sender()
        ok, err = sender.send(text)
        if ok:
            store.set_state(last_ping_key, now.isoformat())
            print(f"[expected_arrivals] notified — {name}")
        else:
            print(f"[expected_arrivals] send failed: {err}", file=sys.stderr)


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


# ─── Todoist pre-render (hallucination guard) ───────────────────────────────
#
# Briefs previously asked the agent to call todoist_list_tasks itself and
# pick top items. With ~40 tasks in scope and a strict "top 3 P1 overdue"
# instruction, the model would confabulate entries to fill the requested
# format (observed 2026-05-11: model invented two "overdue from May 1"
# tasks that don't exist). Fix: fetch the Todoist data here in Python,
# group by (priority, status), and inject as an authoritative block in
# the synthetic prompt. The agent's job becomes picking which N items
# from a known-good list to surface — no opportunity to invent names
# or due dates.


def _fetch_todoist_for_brief(filter_query: str) -> list[dict[str, Any]]:
    """Pull tasks matching `filter_query` from Todoist with pagination.

    Returns [] on any error so a brief still fires (with an empty Todoist
    block) rather than the whole trigger dropping. The Todoist sub-agent
    is still loaded into the SDK, so the agent could in principle retry
    via its own tool call — though the prompt tells it not to.
    """
    key = (os.environ.get("TODOIST_API_KEY") or "").strip()
    if not key:
        return []
    headers = {"Authorization": f"Bearer {key}"}
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"query": filter_query, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(
                "https://api.todoist.com/api/v1/tasks/filter",
                headers=headers, params=params, timeout=15,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[todoist pre-render] fetch failed: {e}", file=sys.stderr)
            return out
        data = r.json()
        out.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return out


def _categorize_task(t: dict[str, Any], today_iso: str) -> tuple[int, str]:
    """Return (api_priority, status). Priority is the raw API value
    (4=P1 urgent, 1=P4 low). Status is 'overdue' / 'today' / 'future'."""
    pri = int(t.get("priority") or 1)
    due_raw = (t.get("due") or {}).get("date") or ""
    # Date-only strings stay as YYYY-MM-DD; datetime strings get truncated.
    due_part = due_raw.split("T")[0] if due_raw else ""
    if not due_part:
        status = "future"
    elif due_part < today_iso:
        status = "overdue"
    elif due_part == today_iso:
        status = "today"
    else:
        status = "future"
    return pri, status


def _render_todoist_block(tasks: list[dict[str, Any]], today_iso: str) -> str:
    """Format tasks as the authoritative Todoist block injected into the
    synthetic prompt. Groups by (status, priority); names are quoted
    verbatim so the agent doesn't get tempted to paraphrase."""
    if not tasks:
        return (
            "TODOIST TASKS:\n"
            "(no tasks matched the brief's filter — the list is empty.)"
        )

    today = date.fromisoformat(today_iso)
    buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for t in tasks:
        pri, status = _categorize_task(t, today_iso)
        buckets.setdefault((status, pri), []).append(t)

    lines = [
        "TODOIST TASKS (authoritative — surface ONLY tasks from this list,",
        "names quoted VERBATIM, IDs preserved, due dates as written here):",
    ]

    def emit(label: str, status: str, pri: int) -> int:
        items = buckets.get((status, pri), [])
        if not items:
            return 0
        lines.append(f"\n{label}:")
        for t in items:
            name = t.get("content") or "(no name)"
            tid = t.get("id") or "?"
            due_raw = (t.get("due") or {}).get("date") or ""
            due_part = due_raw.split("T")[0] if due_raw else ""
            if status == "overdue" and due_part:
                try:
                    days = (today - date.fromisoformat(due_part)).days
                    extra = f" — due {due_part} ({days} day{'s' if days != 1 else ''} overdue)"
                except ValueError:
                    extra = f" — due {due_part}"
            elif status == "today":
                extra = " — due today"
            elif status == "future" and due_part:
                extra = f" — due {due_part}"
            else:
                extra = ""
            lines.append(f'- "{name}"{extra} [id: {tid}]')
        return len(items)

    # High-priority first — these are the ones the agent should lead with.
    emit("OVERDUE P1 (urgent, past due)", "overdue", 4)
    emit("OVERDUE P2 (high, past due)", "overdue", 3)
    emit("DUE TODAY P1 (urgent)", "today", 4)
    emit("DUE TODAY P2 (high)", "today", 3)

    # Lower-priority aggregated; enumerating them just gives the model
    # more confabulation surface for things it shouldn't be surfacing.
    lower_today = len(buckets.get(("today", 2), [])) + len(buckets.get(("today", 1), []))
    lower_overdue = len(buckets.get(("overdue", 2), [])) + len(buckets.get(("overdue", 1), []))
    if lower_today or lower_overdue:
        parts = []
        if lower_overdue:
            parts.append(f"{lower_overdue} overdue")
        if lower_today:
            parts.append(f"{lower_today} due today")
        lines.append(
            f"\nLOWER PRIORITY (P3/P4): {lower_overdue + lower_today} items "
            f"({', '.join(parts)}) — omit specifics from the brief; mention "
            "only if the high-priority sections are empty."
        )

    return "\n".join(lines)


def _snapshot_key(trigger_name: str) -> str:
    """State key for the per-trigger overdue-P1 snapshot."""
    return f"brief_snapshot_{trigger_name}"


def _current_overdue_p1_ids(tasks: list[dict[str, Any]], today_iso: str) -> list[str]:
    """Return the IDs of tasks that are P1 (api priority 4) and overdue."""
    out: list[str] = []
    for t in tasks:
        pri, status = _categorize_task(t, today_iso)
        if pri == 4 and status == "overdue":
            tid = t.get("id")
            if tid:
                out.append(str(tid))
    return out


def _compute_progress_diff(
    store: MemoryStore | None,
    trigger_name: str,
    current_ids: list[str],
) -> dict[str, int] | None:
    """Compare current overdue-P1 IDs to the last snapshot. Returns a small
    dict with prev/current counts and cleared count, or None if there's
    no prior snapshot (first run) or store is unavailable."""
    if store is None:
        return None
    raw = store.get_state(_snapshot_key(trigger_name))
    if not raw:
        return None
    try:
        prev = json.loads(raw)
    except json.JSONDecodeError:
        return None
    prev_ids = set(prev.get("overdue_p1_ids") or [])
    if not prev_ids:
        return None
    cur_ids = set(current_ids)
    cleared = prev_ids - cur_ids
    # Only emit a diff line if something actually changed; an all-noise
    # "0 cleared" doesn't deserve airtime.
    if not cleared:
        return None
    return {
        "prev_count": len(prev_ids),
        "current_count": len(cur_ids),
        "cleared_count": len(cleared),
    }


def _save_brief_snapshot(
    store: MemoryStore | None,
    trigger_name: str,
    current_ids: list[str],
) -> None:
    if store is None:
        return
    payload = {
        "overdue_p1_ids": current_ids,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    store.set_state(_snapshot_key(trigger_name), json.dumps(payload))


# ─── Deliveries pre-render (for morning brief) ─────────────────────────────


# delivery_today facts are stored with this content shape:
#   "carrier=X subject=Y tracking=Z tracking_url=W message_id=... ..."
# We parse them back into a dict so the brief block can render cleanly.
_DELIVERY_FACT_KEYS = (
    "carrier", "subject", "tracking", "tracking_url",
    "message_id", "thread_id", "from", "reason", "alerted_at",
)
_DELIVERY_FACT_PATTERN = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|$)")


def _parse_delivery_fact(content: str) -> dict[str, str]:
    """Parse a delivery_today fact's content string back into key=value pairs.

    Values may contain spaces (the subject especially), so we walk the
    string looking for `key=` boundaries. Unknown keys are dropped.
    """
    out: dict[str, str] = {}
    for m in _DELIVERY_FACT_PATTERN.finditer(content):
        k, v = m.group(1), m.group(2).strip()
        if k in _DELIVERY_FACT_KEYS:
            out[k] = v
    return out


def _render_deliveries_block(store: MemoryStore, hours: int = 24) -> str:
    """Format recent delivery_today facts as an authoritative brief block.

    Only facts alerted in the last `hours` are included — older
    deliveries are stale by morning. Returns "" if there's nothing
    recent to surface.
    """
    facts = store.recall_facts(category="delivery_today", limit=30)
    if not facts:
        return ""

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent: list[dict[str, str]] = []
    for f in facts:
        created_raw = f.get("created_at") or ""
        try:
            created = datetime.fromisoformat(created_raw)
        except ValueError:
            continue
        if created < cutoff:
            continue
        info = _parse_delivery_fact(f.get("content") or "")
        if info:
            recent.append(info)

    if not recent:
        return ""

    lines = [
        "DELIVERIES TODAY (authoritative — surface ONLY these,",
        "include the tracking URL inline so it's tappable):",
    ]
    for d in recent:
        carrier = d.get("carrier", "?")
        subj = d.get("subject", "(no subject)")
        line = f'- {carrier}: "{subj}"'
        tracking = d.get("tracking", "")
        if tracking:
            line += f" — tracking: {tracking}"
        lines.append(line)
        url = d.get("tracking_url", "")
        if url:
            lines.append(f"  {url}")

    return "\n".join(lines)


def _render_email_triage_block(store: MemoryStore) -> str:
    """Visibility line for how many emails got sent to Anthropic for
    triage in the last 24h. Surfaces the data flow into the morning
    brief so the user knows what they're paying for in privacy terms.

    Returns "" when nothing was triaged in the window (no need for a
    line about 0 emails). ROADMAP M4.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        rows = store._conn().execute(  # noqa: SLF001 — internal helper
            "SELECT payload FROM api_events "
            "WHERE kind = 'email_triage_run' AND timestamp >= ? "
            "ORDER BY id DESC",
            (cutoff,),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"[email triage block] query failed: {e}", file=sys.stderr)
        return ""
    classified = 0
    flagged = 0
    for r in rows:
        try:
            data = json.loads(r["payload"])
            classified += int(data.get("classified", 0))
            flagged += int(data.get("flagged", 0))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    if classified <= 0:
        return ""
    # Compact one-liner. The brief prompt is instructed to render it
    # verbatim near the bottom of the brief (or skip it entirely if
    # it's not present in the injected block).
    return (
        f"📧 triaged {classified} email(s) to Anthropic in the last "
        f"24h ({flagged} flagged)"
    )


def _render_sleep_block() -> str:
    """Pull last-night Eight Sleep data and format as a brief block.

    Returns "" if Eight Sleep isn't configured (no env creds) OR the
    API call fails. Eight Sleep is an UNOFFICIAL API and can break
    without notice — failure here must never block the brief itself,
    just silently drop the sleep section.
    """
    if not (os.environ.get("EIGHT_EMAIL") or "").strip():
        return ""
    # Password may live in the macOS Keychain (ROADMAP H5), so don't
    # gate on EIGHT_PASSWORD env presence — let the auth resolver in
    # mcp_servers.eightsleep_auth raise if neither location has it.
    try:
        from mcp_servers.eightsleep_auth import auth_headers, user_id  # noqa: E402

        uid = user_id()
        r = requests.get(
            f"https://client-api.8slp.net/v1/users/{uid}/intervals",
            headers=auth_headers(),
            timeout=15,
        )
        r.raise_for_status()
        intervals = (r.json() or {}).get("intervals") or []
    except Exception as e:  # noqa: BLE001
        print(f"[sleep block] eight sleep fetch failed: {e}", file=sys.stderr)
        return ""
    if not intervals:
        return ""
    latest = intervals[0]

    def _avg(d: dict[str, Any] | None, *keys: str) -> Any:
        if not isinstance(d, dict):
            return None
        for k in keys:
            v = d.get(k)
            if v is not None:
                return v
        return None

    score = latest.get("score") or _avg(latest.get("sleepFitnessScore"), "total")
    duration_s = latest.get("duration") or latest.get("totalSleep") or 0
    hr = _avg(latest.get("heartRate"), "avg", "average")
    hrv = _avg(latest.get("hrv"), "avg", "average")
    resp = _avg(latest.get("respiratoryRate"), "avg", "average")
    bed_temp = _avg(latest.get("bedTemperature"), "avg", "average")

    lines = ["LAST NIGHT'S SLEEP (Eight Sleep — for brief context only):"]
    if duration_s:
        minutes = int(duration_s) // 60
        if minutes >= 60:
            h, m = divmod(minutes, 60)
            lines.append(f"  time asleep: {h}h {m}m" if m else f"  time asleep: {h}h")
        else:
            lines.append(f"  time asleep: {minutes}m")
    if score is not None:
        lines.append(f"  sleep score: {score}")
    if hr is not None:
        lines.append(f"  avg HR: {hr:.0f} bpm")
    if hrv is not None:
        lines.append(f"  avg HRV: {hrv:.0f} ms")
    if resp is not None:
        lines.append(f"  avg respiratory rate: {resp:.1f} /min")
    if bed_temp is not None:
        lines.append(f"  avg bed temp: {bed_temp:.1f}°F")
    if len(lines) == 1:
        return ""  # nothing usable
    return "\n".join(lines)


def _todoist_block_for(trigger_name: str, store: MemoryStore | None = None) -> str:
    """Choose the right filter per trigger, pre-render, and prepend a
    progress note if overdue P1s were cleared since the last fire."""
    if trigger_name == "morning_brief":
        filter_query = "today | overdue"
    elif trigger_name == "weekly_review":
        filter_query = "overdue"
    else:
        return ""

    tasks = _fetch_todoist_for_brief(filter_query)
    today_iso = datetime.now(_user_tz()).date().isoformat()

    # Diff vs last snapshot — used by the brief prompt to emit a progress
    # opener like "overdue cleared from 4 to 2, nice work."
    current_ids = _current_overdue_p1_ids(tasks, today_iso)
    diff = _compute_progress_diff(store, trigger_name, current_ids)
    _save_brief_snapshot(store, trigger_name, current_ids)

    block = _render_todoist_block(tasks, today_iso)

    if diff:
        progress_line = (
            f"PROGRESS SINCE LAST BRIEF: {diff['cleared_count']} overdue P1 cleared "
            f"(was {diff['prev_count']}, now {diff['current_count']}). "
            "If this is a meaningful drop, lead the brief with a short "
            "conversational acknowledgement (e.g. 'overdue cleared from "
            f"{diff['prev_count']} to {diff['current_count']}, nice'). "
            "Skip the opener if the drop is trivial (1) or if it'd feel forced.\n\n"
        )
        block = progress_line + block

    return block


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

    # Pre-render Todoist data deterministically and append to the synthetic
    # prompt as an authoritative block. The store is threaded in so the
    # block can include a progress diff vs the last fire ("overdue cleared
    # from 4 to 2"). The brief PROMPTS tell the agent to surface tasks
    # ONLY from this block, with light paraphrase allowed.
    todoist_block = _todoist_block_for(trigger_name, store=store)
    if todoist_block:
        prompt = f"{prompt}\n\n--- INJECTED TODOIST DATA ---\n{todoist_block}\n--- END TODOIST DATA ---"

    # Pre-render today's delivery rollup for the morning brief — pulled
    # from delivery_today facts logged by _fire_delivery_watch over the
    # last 24h. Same authoritative-block pattern: agent surfaces only
    # what's here, including the tracking URL on its own line so iMessage
    # auto-links it.
    if trigger_name == "morning_brief":
        deliveries_block = _render_deliveries_block(store)
        if deliveries_block:
            prompt = (
                f"{prompt}\n\n--- INJECTED DELIVERIES DATA ---\n"
                f"{deliveries_block}\n--- END DELIVERIES DATA ---"
            )
        sleep_block = _render_sleep_block()
        if sleep_block:
            prompt = (
                f"{prompt}\n\n--- INJECTED SLEEP DATA ---\n"
                f"{sleep_block}\n--- END SLEEP DATA ---"
            )
        triage_block = _render_email_triage_block(store)
        if triage_block:
            prompt = (
                f"{prompt}\n\n--- INJECTED EMAIL TRIAGE STATS ---\n"
                f"Render this line verbatim at the very bottom of the "
                f"brief, on its own line, exactly as written:\n"
                f"{triage_block}\n--- END EMAIL TRIAGE STATS ---"
            )
    # Triggers run on Opus (stronger long-context fidelity) — the relay
    # stays on Sonnet for cost.
    options = build_options(store, model=TRIGGER_MODEL)

    conversation_id = store.open_conversation(
        source=CONVERSATION_SOURCE, metadata={"trigger": trigger_name}
    )
    print(f"[fire @ {datetime.now().isoformat()}] {trigger_name} (conv={conversation_id}, model={TRIGGER_MODEL})")

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
        print(f"[sent] {trigger_name}: {reply[:20]}")
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

        # Delivery watch — same throttled-poll pattern as email_watch but
        # specific to carrier "out for delivery / delivered" emails. Gated
        # by `delivery_watch.enabled`.
        try:
            _fire_delivery_watch(store, config, now)
        except Exception as e:  # noqa: BLE001
            print(f"[delivery_watch error] {e}", file=sys.stderr)

        # Expected arrivals — gap detection for emails you expect to
        # receive ahead of known upcoming events. Cadence-throttled
        # internally (default 12h) so it's cheap to call on every tick.
        try:
            _fire_expected_arrivals(store, config, now)
        except Exception as e:  # noqa: BLE001
            print(f"[expected_arrivals error] {e}", file=sys.stderr)

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
            print(
                "usage: --run-now <morning_brief|weekly_review|"
                "delivery_watch|email_watch|expected_arrivals>",
                file=sys.stderr,
            )
            sys.exit(2)
        trigger = sys.argv[idx + 1]
        if trigger == "delivery_watch":
            # delivery_watch isn't an LLM-driven brief — fire the
            # Python notifier directly. Clear the seen-set first so an
            # already-alerted email re-fires (useful for testing tracking
            # extraction against the same email).
            store = MemoryStore()
            store.set_state(_DELIVERY_WATCH_SEEN_KEY, "[]")
            store.set_state(_DELIVERY_WATCH_LAST_CHECK_KEY, "")
            config = _load_config()
            _fire_delivery_watch(store, config, datetime.now(timezone.utc))
            return
        if trigger == "email_watch":
            store = MemoryStore()
            store.set_state(_EMAIL_WATCH_SEEN_KEY, "[]")
            store.set_state(_EMAIL_WATCH_LAST_CHECK_KEY, "")
            config = _load_config()
            _fire_email_watch(store, config, datetime.now(_user_tz()))
            return
        if trigger == "expected_arrivals":
            store = MemoryStore()
            store.set_state(_EXPECTED_ARRIVALS_LAST_CHECK_KEY, "")
            # Clear per-watch daily-ping throttles so a manual run
            # re-pings on the same day.
            rows = store._conn().execute(  # noqa: SLF001
                "SELECT key FROM state WHERE key LIKE ?",
                (f"{_EXPECTED_ARRIVALS_LAST_PING_PREFIX}%",),
            ).fetchall()
            for row in rows:
                store.set_state(row["key"], "")
            config = _load_config()
            _fire_expected_arrivals(store, config, datetime.now(_user_tz()))
            return
        if trigger not in PROMPTS:
            print(
                f"unknown trigger: {trigger}. valid: "
                f"{[*PROMPTS, 'delivery_watch', 'email_watch', 'expected_arrivals']}",
                file=sys.stderr,
            )
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
