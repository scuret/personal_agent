"""Interactive installer / configurator for personal_agent.

Run this from install.sh, or directly via `python -m tools.install`.
Reentrant — safe to re-run to add new sub-agents or fix config later.

Walks through:
  1. Migration check — copy .env, config/, data/ from another path?
  2. Required core — ANTHROPIC_API_KEY.
  3. Sub-agent selection — for each optional integration, enable + key.
  4. Google OAuth — credentials.json + first-auth flow if Gmail or
     Calendar selected.
  5. iMessage relay — mode + target_phone_number + self_handles.
  6. LaunchAgents — offer to install all four (relay, scheduler,
     log-rotation, webui) so the daemons auto-start on login.

Existing values in .env are preserved unless the user explicitly
changes them. Empty input at a prompt = keep existing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

V1_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = V1_DIR / ".env"
ENV_EXAMPLE_PATH = V1_DIR / ".env.example"
DATA_DIR = V1_DIR / "data"
CONFIG_DIR = V1_DIR / "config"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
TRIGGERS_PATH = CONFIG_DIR / "triggers.yaml"
TRIGGERS_EXAMPLE_PATH = CONFIG_DIR / "triggers.yaml.example"


# ─── Sub-agent registry ─────────────────────────────────────────────────────


@dataclass
class SubAgent:
    name: str
    description: str               # short label / one-line headline
    # 2-4 sentence paragraph: what the user can DO with this sub-agent
    # day-to-day AND how it ties into the rest of the agent (morning
    # brief, scheduler, vision flow, other sub-agents). Surfaced in the
    # web install wizard's sub-agent picker so users can decide whether
    # to enable it based on product-level value, not just integration
    # surface. Empty `capabilities` falls back to `description` in the
    # UI but every entry below populates it.
    capabilities: str = ""
    env_vars: list[str] = field(default_factory=list)
    needs_google_oauth: bool = False
    auth_help: str = ""           # one-liner shown when prompting
    setup_url: str = ""            # link to where the user gets the key
    always_on: bool = False        # no auth → can't be turned off


SUBAGENTS: list[SubAgent] = [
    SubAgent(
        "memory",
        "Conversation archive + extracted facts + audit log",
        capabilities=(
            "Persists every conversation, every fact the agent extracts about you, "
            "and a verbatim audit log of every Claude API call. Powers semantic "
            "recall ('remember when we talked about X'), the morning brief's "
            "context injection, and the analytics dashboard. Stored locally in "
            "data/memory.sqlite — no cloud."
        ),
        always_on=True,
    ),
    SubAgent(
        "weather",
        "Open-Meteo current + forecast",
        capabilities=(
            "Current conditions + multi-day forecast for any location (uses your "
            "USER_HOME_ADDRESS as default). Free, keyless. Woven into the morning "
            "brief as a one-line forecast for the day."
        ),
        always_on=True,
    ),
    SubAgent(
        "vision",
        "Image analysis on attachments",
        capabilities=(
            "Describes images you attach in any transport. iMessage HEIC photos "
            "are auto-converted to JPEG, web-chat uploads route through the same "
            "path. The agent calls this whenever it sees an [attachment: image] "
            "marker in your message. Required for any image-aware conversation."
        ),
        always_on=True,
    ),
    SubAgent(
        "wikipedia",
        "Search + read articles",
        capabilities=(
            "Quick factual lookups without leaving the chat — 'who wrote X?', "
            "'when was Y?'. Returns full article summaries or specific section "
            "extracts. Always-on, no key needed."
        ),
        always_on=True,
    ),
    SubAgent(
        "reddit",
        "Public-read subreddits / search / posts",
        capabilities=(
            "Reads top / hot / new posts from any public subreddit, plus comment "
            "threads. Useful for 'what's r/news saying right now' style questions. "
            "Read-only (no posting, no auth needed)."
        ),
        always_on=True,
    ),
    SubAgent(
        "reminders",
        "Schedule 'remind me at 4pm' pings",
        capabilities=(
            "Schedules one-off and recurring (daily / weekdays / weekly / monthly) "
            "reminders that fire over your active transport. Just say 'remind me "
            "to take out the trash at 7pm' and the scheduler handles delivery. "
            "Stored locally; no external dep."
        ),
        always_on=True,
    ),
    # Apple-native (AppleScript). always_on=True because they need no
    # auth, but they're only registered on macOS by agent_host's
    # _is_macos gate — on Linux/Windows the agent won't see these tools.
    SubAgent(
        "reminders_apple",
        "Apple Reminders.app (macOS)",
        capabilities=(
            "List, create, complete, and delete items in any of your Apple "
            "Reminders lists. Syncs to iPhone via iCloud automatically — set "
            "from your Mac, see it on your phone. Distinct from the always-on "
            "`reminders` sub-agent (which is the agent's own scheduler)."
        ),
        always_on=True,
    ),
    SubAgent(
        "notes_apple",
        "Apple Notes.app (macOS)",
        capabilities=(
            "Search by title, read existing notes, append text to them, or "
            "create new ones. The agent can stash a quick thought to a 'running "
            "list' note or pull context out of an existing one. iCloud syncs "
            "the result to your phone."
        ),
        always_on=True,
    ),
    SubAgent(
        "photos_apple",
        "Apple Photos.app (macOS, read-only)",
        capabilities=(
            "List albums, find photos in a date range, get album contents. "
            "Read-only — face / object / place ML tags aren't reachable via "
            "AppleScript, so 'find photos of the kids' won't work, but 'photos "
            "from last Tuesday' will."
        ),
        always_on=True,
    ),
    SubAgent(
        "music_apple",
        "Apple Music.app (macOS)",
        capabilities=(
            "Now-playing, play/pause/next, search-and-play, list playlists. "
            "Controls Music.app on THIS Mac specifically (not phone playback). "
            "Coexists with Spotify — the agent picks based on your phrasing "
            "('play X on Apple Music' vs 'queue X on Spotify')."
        ),
        always_on=True,
    ),
    SubAgent(
        "mail_apple",
        "Apple Mail.app (macOS, drafts only)",
        capabilities=(
            "List accounts + inboxes, search messages, read, draft replies, "
            "draft new messages. NEVER sends — same hard safety rule as Gmail. "
            "Useful if you read mail in Mail.app instead of (or alongside) "
            "Gmail web."
        ),
        always_on=True,
    ),
    SubAgent(
        "maps",
        "Places, drive times, geocoding",
        capabilities=(
            "Search places near a location, get drive time between two "
            "addresses, geocode and reverse-geocode. Useful for 'how long to "
            "Annie Gunn's?', 'find a coffee shop near the office', or planning "
            "an evening route. Uses Google Maps when GOOGLE_MAPS_API_KEY is "
            "set (better quality, costs money); falls back to free "
            "OpenStreetMap (Nominatim + OSRM) otherwise."
        ),
        env_vars=["GOOGLE_MAPS_API_KEY"],
        setup_url="https://console.cloud.google.com/apis/credentials",
        auth_help=(
            "Optional — leave GOOGLE_MAPS_API_KEY blank to use the free "
            "OpenStreetMap fallback (Nominatim + OSRM, no auth). For "
            "Google: enable Places API, Geocoding API, and Distance "
            "Matrix API on a Google Cloud project, then create an API "
            "key. Free tier covers personal use; needs a billing account "
            "on file."
        ),
    ),
    SubAgent(
        "eightsleep",
        "Eight Sleep — sleep metrics + bed temp",
        capabilities=(
            "Pulls last night's sleep score, HRV, resting heart rate, time "
            "slept, and current bed-side temp; can also set temp by side. "
            "The morning brief opens with a one-line sleep summary when this "
            "is configured ('slept 7h22, HRV 48 — solid'). UNOFFICIAL API — "
            "could break if Eight Sleep changes endpoints, but failures are "
            "isolated so the rest of the agent keeps running."
        ),
        env_vars=["EIGHT_EMAIL"],
        setup_url="https://www.eightsleep.com",
        auth_help=(
            "Email from your Eight Sleep account in .env. Password lives "
            "in the macOS Keychain — run "
            "`python -m tools.eightsleep_set_password` after this step "
            "to store it. If you can't use Keychain (Linux/Windows), "
            "set EIGHT_PASSWORD in .env directly. UNOFFICIAL API — "
            "could break if Eight Sleep changes endpoints; sub-agent "
            "isolates failures from the rest of the agent."
        ),
    ),
    SubAgent(
        "todoist",
        "Task management",
        capabilities=(
            "Capture tasks from any conversation ('remind me to call the "
            "dentist'), list today / overdue / by project / by label, "
            "complete from chat. Powers the 'top tasks' section of the "
            "morning brief (with a hallucination-guard injected block so "
            "the agent only surfaces real items) and the Sunday weekly "
            "review's 'incomplete tasks last week' rollup."
        ),
        env_vars=["TODOIST_API_KEY"],
        setup_url="https://todoist.com/app/settings/integrations/developer",
    ),
    SubAgent(
        "gmail",
        "Gmail — read, draft (NEVER sends)",
        capabilities=(
            "Read + search your inbox, draft replies, apply labels, archive "
            "threads. NEVER sends — drafts only, same hard rule as Apple Mail. "
            "Every email-triage decision in the morning brief flows through "
            "here, and the agent recalls past 'alerted_email' facts when you "
            "say 'draft a response to that one from Sarah.'"
        ),
        needs_google_oauth=True,
        setup_url="https://console.cloud.google.com (enable Gmail API + create Desktop OAuth client)",
    ),
    SubAgent(
        "calendar",
        "Google Calendar — read + write",
        capabilities=(
            "List today's events, search, check free/busy, and create / update "
            "/ delete events. The morning brief opens with today's calendar; "
            "the agent schedules events when you ask ('book Annie's at 6pm "
            "Friday'). Reads multiple calendars."
        ),
        needs_google_oauth=True,
        setup_url="https://console.cloud.google.com (enable Calendar API)",
    ),
    SubAgent(
        "drive",
        "Google Drive — search + read + share",
        capabilities=(
            "Search across your Drive, browse folders, read text-file content, "
            "create shareable links. The agent can pull a contract or spec into "
            "context ('what's in the Q3 plan doc?') or hand you a sharable link "
            "without opening Drive."
        ),
        needs_google_oauth=True,
        setup_url="https://console.cloud.google.com (enable Drive API)",
    ),
    SubAgent(
        "docs",
        "Google Docs — read, append, find-and-replace, create",
        capabilities=(
            "Read full document text, append paragraphs, find-and-replace, "
            "create new docs. The agent can stash a meeting summary into a "
            "long-running doc or draft a memo for you to polish."
        ),
        needs_google_oauth=True,
        setup_url="https://console.cloud.google.com (enable Docs API)",
    ),
    SubAgent(
        "sheets",
        "Google Sheets — read + append + update + create",
        capabilities=(
            "Read a range, append rows, update specific cells, create new "
            "sheets. Useful for 'log this expense to my tracker' or 'what's "
            "this month's spend so far?' style flows."
        ),
        needs_google_oauth=True,
        setup_url="https://console.cloud.google.com (enable Sheets API)",
    ),
    SubAgent(
        "notion",
        "Notion — pages, DBs, append + create",
        capabilities=(
            "Search pages, read content, query databases, append to existing "
            "pages, create new ones. The agent can stash conversation "
            "summaries to a 'notes' database, pull project context into a "
            "brief, or capture an idea to your inbox page. Each page or DB "
            "must be explicitly shared with the integration in Notion."
        ),
        env_vars=["NOTION_INTEGRATION_TOKEN"],
        setup_url="https://www.notion.so/profile/integrations",
        auth_help="Internal integration token. Also share each page/db with the integration.",
    ),
    SubAgent(
        "github",
        "GitHub — repos, issues, PRs",
        capabilities=(
            "Browse repos, list / read / create issues, list PRs, search code "
            "across repos you have access to. Useful when the agent's helping "
            "triage bug reports, recall what you shipped last week, or open a "
            "quick issue from chat ('file a bug against frontend: login is "
            "broken')."
        ),
        env_vars=["GITHUB_TOKEN"],
        setup_url="https://github.com/settings/tokens",
        auth_help="Classic with `repo` scope OR fine-grained with Issues r+w / PRs r / Contents r / Metadata r.",
    ),
    SubAgent(
        "web",
        "Web search (Brave) + URL fetch",
        capabilities=(
            "Search the live web + fetch the text contents of any URL. The "
            "agent uses this for anything recent or specific that's outside "
            "Claude's training data — current news, today's pricing, a "
            "specific blog post you reference. Free tier covers 2K queries/mo."
        ),
        env_vars=["BRAVE_SEARCH_API_KEY"],
        setup_url="https://api.search.brave.com",
        auth_help="Subscribe → Free tier (2K queries/month). Requires a credit card on file.",
    ),
    SubAgent(
        "youtube",
        "YouTube — search + metadata",
        capabilities=(
            "Search videos and channels, look up titles / view counts / "
            "publish dates. Public-read only (no playback control, no "
            "subscription management). Useful for 'find me a tutorial on X' "
            "or 'what's the top video from <channel> this week.'"
        ),
        env_vars=["YOUTUBE_API_KEY"],
        setup_url="https://console.cloud.google.com (enable YouTube Data API v3 → API key)",
        auth_help="Just an API key, NOT OAuth. Free quota: 10K units/day.",
    ),
    SubAgent(
        "dropbox",
        "Dropbox — search, read, share",
        capabilities=(
            "Search filenames, list folders, read text-file contents, "
            "generate share links. The agent can pull a document into "
            "context ('what's in the contract from last week?') or hand "
            "you a shareable link without opening the Dropbox app. Text-"
            "file content only — no Word/PDF parsing in v1."
        ),
        env_vars=["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET"],
        setup_url="https://www.dropbox.com/developers/apps",
        auth_help=(
            "Scoped Access app. Permissions: files.metadata.read, "
            "files.content.read, sharing.read. Settings tab → ADD "
            "http://localhost:53682 to Redirect URIs (exact match). "
            "Copy App key and App secret. Then run "
            "`python -m mcp_servers.dropbox_auth` once to grant access "
            "and seed the refresh token."
        ),
    ),
    SubAgent(
        "spotify",
        "Spotify — search, playback, playlists",
        capabilities=(
            "Search tracks / albums / artists, queue and play music, manage "
            "playlists, list devices. Useful for ambient requests like 'put "
            "on focus music' or 'queue the playlist from last Friday.' "
            "Playback control requires a Spotify Premium account. Coexists "
            "with Apple Music — agent picks based on your phrasing."
        ),
        env_vars=["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"],
        setup_url="https://developer.spotify.com/dashboard",
        auth_help=(
            "Create app → set Redirect URI to http://127.0.0.1:8765 "
            "exactly. Copy Client ID + Client Secret. Then run "
            "`python -m mcp_servers.spotify_auth` once to grant access "
            "and seed the refresh token. Playback control needs Premium."
        ),
    ),
    SubAgent(
        "canva",
        "Canva — search, create, export designs",
        capabilities=(
            "Search your designs, fetch metadata, create new designs from "
            "templates, export to PNG / PDF, manage folders. Useful when "
            "you're iterating on visual content alongside the conversation "
            "('whip up a quick poster for...'). Lower daily value than the "
            "productivity integrations."
        ),
        env_vars=["CANVA_CLIENT_ID", "CANVA_CLIENT_SECRET"],
        setup_url="https://developer.canva.com",
        auth_help=(
            "Create integration → Authentication → add Redirect URL "
            "EXACTLY http://127.0.0.1:8767. Configure scopes "
            "(design:meta:read, design:content:read, "
            "design:content:write, folder:read, asset:read, "
            "profile:read). Copy Client ID + Client Secret. Then run "
            "`python -m mcp_servers.canva_auth` once to grant access "
            "and seed the refresh token."
        ),
    ),
    SubAgent(
        "linkedin",
        "LinkedIn — profile + text posts (narrow)",
        capabilities=(
            "Read your own profile + post short text updates. Narrow API "
            "surface — most useful endpoints (search, company pages, full "
            "feed) are restricted to Marketing/Talent partner apps that "
            "personal accounts can't access. Skip unless you specifically "
            "want 'post my draft to LinkedIn' as a workflow."
        ),
        env_vars=["LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"],
        setup_url="https://www.linkedin.com/developers/apps",
        auth_help=(
            "Create app (requires a LinkedIn Page you admin) → Auth "
            "tab → add Authorized redirect URL EXACTLY "
            "http://127.0.0.1:8768. Products tab → request 'Sign In "
            "with LinkedIn using OpenID Connect' + 'Share on LinkedIn' "
            "(both auto-approved). Copy Client ID + Client Secret. "
            "Then run `python -m mcp_servers.linkedin_auth` once. "
            "NOTE: personal-tier tokens don't refresh — re-run every "
            "~55 days to keep the agent connected."
        ),
    ),
]


# ─── Pretty-print helpers ───────────────────────────────────────────────────


def _hr(label: str = "") -> None:
    width = 70
    if label:
        line = f"── {label} "
        line += "─" * max(width - len(line), 1)
    else:
        line = "─" * width
    print(line)


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}")


def _ask(prompt: str, default: str | None = None) -> str:
    """Prompt for a single line. Empty input returns the default."""
    if default:
        # Mask anything that looks like a credential
        shown = default
        if len(default) > 30:
            shown = default[:6] + "…" + default[-4:]
        elif default.startswith(("sk-", "ghp_", "ntn_", "secret_", "AIza", "BSAGE", "sl.u.")):
            shown = default[:4] + "…(masked)"
        full_prompt = f"{prompt} [keep current: {shown}] "
    else:
        full_prompt = f"{prompt} "
    raw = input(full_prompt).strip()
    return raw if raw else (default or "")


def _yn(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{prompt} {suffix} ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


# ─── .env handling ──────────────────────────────────────────────────────────


def _read_env() -> dict[str, str]:
    """Parse the existing .env (if any) into a dict. Order isn't preserved."""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", s)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _write_env(values: dict[str, str]) -> None:
    """Re-emit .env using .env.example as the template, substituting values.

    Anything in `values` overrides the placeholder. Anything not in
    .env.example but present in `values` is appended at the bottom under
    a "# additional" header.
    """
    if not ENV_EXAMPLE_PATH.exists():
        # Fallback: write key=value pairs in arbitrary order.
        ENV_PATH.write_text(
            "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
            encoding="utf-8",
        )
        return

    out_lines: list[str] = []
    seen_keys: set[str] = set()
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", line)
        if m:
            key = m.group(1)
            seen_keys.add(key)
            v = values.get(key, "")
            out_lines.append(f"{key}={v}")
        else:
            out_lines.append(line)

    extras = sorted(set(values.keys()) - seen_keys)
    if extras:
        out_lines.append("")
        out_lines.append("# ─── Additional values not in .env.example ─────────────────────────")
        for k in extras:
            out_lines.append(f"{k}={values[k]}")

    ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)  # readable only by owner


# ─── Step 0 — Migration ─────────────────────────────────────────────────────


def step_migration(env: dict[str, str]) -> None:
    _hr("0. Migrate from another install?")
    print("If you're moving this app from another machine, you can copy")
    print("its .env, config/credentials.json, and data/memory.sqlite over")
    print("here to preserve API keys and conversation history.")
    print()
    if not _yn("Migrate from an existing install?", default=False):
        return
    src = _ask("Path to the OTHER install's v1/ directory (or empty to skip):")
    if not src:
        return
    src_dir = Path(src).expanduser().resolve()
    if not src_dir.is_dir():
        _err(f"not a directory: {src_dir}")
        return

    # .env
    src_env = src_dir / ".env"
    if src_env.exists():
        if _yn(f"Copy .env from {src_env}?", default=True):
            shutil.copy2(src_env, ENV_PATH)
            os.chmod(ENV_PATH, 0o600)
            env.clear()
            env.update(_read_env())
            _ok("copied .env")

    # credentials.json
    src_creds = src_dir / "config" / "credentials.json"
    if src_creds.exists() and not CREDENTIALS_PATH.exists():
        if _yn(f"Copy config/credentials.json from {src_creds}?", default=True):
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_creds, CREDENTIALS_PATH)
            _ok("copied credentials.json")

    # google_token.pickle
    src_token = src_dir / "data" / "google_token.pickle"
    dst_token = DATA_DIR / "google_token.pickle"
    if src_token.exists() and not dst_token.exists():
        if _yn(f"Copy cached Google OAuth token from {src_token}?", default=True):
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_token, dst_token)
            _ok("copied google_token.pickle")

    # memory.sqlite
    src_db = src_dir / "data" / "memory.sqlite"
    dst_db = DATA_DIR / "memory.sqlite"
    if src_db.exists():
        if dst_db.exists():
            _warn("data/memory.sqlite already exists here — skipping (manually copy if you want to overwrite)")
        else:
            if _yn(f"Copy conversation archive from {src_db}?", default=True):
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_db, dst_db)
                _ok("copied memory.sqlite")

    # triggers.yaml — gitignored so it has to be copied during migration
    src_trig = src_dir / "config" / "triggers.yaml"
    if src_trig.exists() and not TRIGGERS_PATH.exists():
        if _yn(f"Copy email-watch / scheduler config from {src_trig}?", default=True):
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_trig, TRIGGERS_PATH)
            _ok("copied triggers.yaml")


# ─── Step 1 — Required core ─────────────────────────────────────────────────


def step_required(env: dict[str, str]) -> None:
    _hr("1. Required: Anthropic API key")
    print("Powers the agent itself. Get one at https://console.anthropic.com")
    val = _ask("ANTHROPIC_API_KEY:", default=env.get("ANTHROPIC_API_KEY", ""))
    if val:
        env["ANTHROPIC_API_KEY"] = val
        _ok("set")
    else:
        _warn("ANTHROPIC_API_KEY is required for the agent to run; you can set it later by re-running this script")


# ─── Step 2 — Sub-agents ────────────────────────────────────────────────────


def step_subagents(env: dict[str, str]) -> set[str]:
    """Walk through optional sub-agents. Returns the set of enabled names."""
    _hr("2. Sub-agent selection")

    # Always-on sub-agents
    always = [s.name for s in SUBAGENTS if s.always_on]
    print(f"Always-on (no auth needed): {', '.join(always)}")
    print()

    enabled: set[str] = set(always)

    optional = [s for s in SUBAGENTS if not s.always_on]
    print("Optional sub-agents — pick which you want enabled.")
    print("(Empty input keeps current state. Each enabled one prompts for its key.)\n")

    for s in optional:
        # Determine current state from env presence
        already_configured = bool(s.env_vars and all(env.get(v) for v in s.env_vars))
        if s.needs_google_oauth:
            already_configured = CREDENTIALS_PATH.exists()
        state = "currently ENABLED" if already_configured else "currently disabled"

        print(f"\n[{s.name}] {s.description} — {state}")
        if s.setup_url:
            print(f"  setup: {s.setup_url}")
        if s.auth_help:
            print(f"  note:  {s.auth_help}")

        enable = _yn(f"Enable {s.name}?", default=already_configured)
        if not enable:
            # If they're disabling, blank out the env vars
            if already_configured and s.env_vars:
                if _yn(f"Clear {', '.join(s.env_vars)} from .env?", default=False):
                    for v in s.env_vars:
                        env[v] = ""
            continue

        enabled.add(s.name)
        for var in s.env_vars:
            current = env.get(var, "")
            new_val = _ask(f"  {var}:", default=current)
            if new_val:
                env[var] = new_val

    return enabled


# ─── Step 3 — Google OAuth ──────────────────────────────────────────────────


def step_google_oauth(env: dict[str, str], enabled: set[str]) -> None:
    needs = ("gmail" in enabled) or ("calendar" in enabled)
    if not needs:
        return

    _hr("3. Google OAuth (Gmail + Calendar)")

    if not CREDENTIALS_PATH.exists():
        print("Need an OAuth client JSON file from Google Cloud Console.")
        print("  1. https://console.cloud.google.com → enable Gmail + Calendar APIs.")
        print("  2. Credentials → Create credentials → OAuth client ID → Desktop app.")
        print("  3. Download the JSON.")
        print()
        path_str = _ask("Path to the downloaded credentials JSON (or empty to skip for now):")
        if path_str:
            src = Path(path_str).expanduser().resolve()
            if not src.is_file():
                _err(f"file not found: {src}")
            else:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, CREDENTIALS_PATH)
                os.chmod(CREDENTIALS_PATH, 0o600)
                _ok(f"copied to {CREDENTIALS_PATH}")
    else:
        _ok(f"credentials.json already at {CREDENTIALS_PATH}")

    if not CREDENTIALS_PATH.exists():
        _warn("Skipping OAuth — re-run this script after placing credentials.json.")
        return

    token_path = DATA_DIR / "google_token.pickle"
    if token_path.exists():
        _ok(f"cached OAuth token already at {token_path}")
        if not _yn("Re-run OAuth flow anyway? (e.g. to grant new scopes)", default=False):
            return

    print()
    print("Running OAuth flow now. A browser will open — grant Gmail + Calendar access.")
    if not _yn("Continue?", default=True):
        return

    try:
        subprocess.run(
            [sys.executable, "-m", "mcp_servers.google_auth"],
            cwd=str(V1_DIR),
            check=True,
        )
        _ok("OAuth flow complete — token cached")
    except subprocess.CalledProcessError as e:
        _err(f"OAuth flow failed: {e}")


# ─── Step 4 — Relay transport ───────────────────────────────────────────────


def step_relay(env: dict[str, str]) -> None:
    _hr("4. Relay transport")
    print("Pick the messaging surface for talking to the agent:")
    print("  imessage  — macOS only. Polls chat.db + sends via AppleScript.")
    print("              Requires Full Disk Access + Automation permissions.")
    print("  telegram  — Cross-platform. Bot via @BotFather, no Mac needed.")
    print("  discord   — Discord bot, DMs + opt-in server channels.")
    print("  slack     — Slack bot in a workspace, DMs + opt-in channels.")
    print("  sms       — Twilio-based SMS. Universal reach but text-only and")
    print("              needs a public webhook URL (ngrok or reverse proxy).")
    print()

    current = env.get("RELAY_TRANSPORT", "imessage")
    transport = _ask(
        "RELAY_TRANSPORT [imessage/telegram/discord/slack/sms]:", default=current
    )
    valid = ("imessage", "telegram", "discord", "slack", "sms")
    if transport not in valid:
        _warn(f"unknown transport {transport!r}, keeping {current}")
        transport = current
    env["RELAY_TRANSPORT"] = transport

    if transport == "imessage":
        _step_imessage_config(env)
    elif transport == "telegram":
        _step_telegram_config(env)
    elif transport == "discord":
        _step_discord_config(env)
    elif transport == "slack":
        _step_slack_config(env)
    elif transport == "sms":
        _step_sms_config(env)


def _step_imessage_config(env: dict[str, str]) -> None:
    print()
    print("─ iMessage config ─")
    print("Mode:")
    print("  self    — listen to your OWN iMessages in note-to-self chats")
    print("            (you text yourself from your iPhone, the agent replies)")
    print("  contact — listen to messages from one specific other contact")
    current_mode = env.get("IMESSAGE_MODE", "self")
    mode = _ask(f"IMESSAGE_MODE [self/contact]:", default=current_mode)
    if mode not in ("self", "contact"):
        _warn(f"unknown mode {mode!r}, keeping {current_mode}")
        mode = current_mode
    env["IMESSAGE_MODE"] = mode

    print()
    if mode == "self":
        print("In self mode TARGET_PHONE_NUMBER is YOUR primary number")
        print("(format: +15555551234 — also the destination for replies).")
    else:
        print("In contact mode TARGET_PHONE_NUMBER is the OTHER person's handle")
        print("(format: +15555551234 or apple-id@icloud.com).")
    env["TARGET_PHONE_NUMBER"] = _ask(
        "TARGET_PHONE_NUMBER:", default=env.get("TARGET_PHONE_NUMBER", "")
    )

    if mode == "self":
        print()
        print("Optional: extra self handles (e.g. your Apple ID email) to also watch.")
        env["SELF_HANDLES"] = _ask(
            "SELF_HANDLES:", default=env.get("SELF_HANDLES", "")
        )

    print()
    print("Optional: group chat support.")
    print("The relay can additionally listen in family / work group chats and")
    print("respond when triggered. Set IMESSAGE_GROUP_CHATS to opt in, leave")
    print("empty to skip. After install, you can discover available groups by")
    print("running:  python -m relay.imessage_relay --check")
    print("Then re-run this installer (or edit .env directly) to allowlist them.")
    current_groups = env.get("IMESSAGE_GROUP_CHATS", "")
    if current_groups:
        print(f"  Currently: {current_groups}")
    env["IMESSAGE_GROUP_CHATS"] = _ask(
        "IMESSAGE_GROUP_CHATS (comma-sep chat_identifier or display_name):",
        default=current_groups,
    )
    if env["IMESSAGE_GROUP_CHATS"].strip():
        print()
        print("Group trigger substrings (case-insensitive). The relay only")
        print("responds when a group message contains one of these. Defaults")
        print("to '@agent, hey agent, agent,' if left blank.")
        env["IMESSAGE_GROUP_TRIGGERS"] = _ask(
            "IMESSAGE_GROUP_TRIGGERS (comma-sep, or empty for defaults):",
            default=env.get("IMESSAGE_GROUP_TRIGGERS", ""),
        )

    print()
    print("Reminder: macOS launchd-spawned processes need their own permissions.")
    print("After install, grant:")
    print("  • Full Disk Access → for the venv's Python binary")
    print("  • Automation → Messages → for the same Python binary")


def _step_telegram_config(env: dict[str, str]) -> None:
    print()
    print("─ Telegram config ─")
    print("Step 1: create the bot")
    print("  Open Telegram, search for @BotFather, send /newbot.")
    print("  Pick a display name and a username (must end in `bot`).")
    print("  BotFather replies with a token like '123456:ABC-DEF...'.")
    print()
    env["TELEGRAM_BOT_TOKEN"] = _ask(
        "TELEGRAM_BOT_TOKEN:", default=env.get("TELEGRAM_BOT_TOKEN", "")
    )

    print()
    print("Step 2: find your Telegram user id")
    print("  Search @userinfobot in Telegram, start it, copy the numeric id")
    print("  it replies with. The bot will ignore anyone NOT in this list.")
    print("  Comma-separated for multiple users.")
    env["TELEGRAM_ALLOWED_USER_IDS"] = _ask(
        "TELEGRAM_ALLOWED_USER_IDS:",
        default=env.get("TELEGRAM_ALLOWED_USER_IDS", ""),
    )

    print()
    print("Optional: chat_id for scheduled briefs/reminders. Leave empty")
    print("to default to the first allowed user id.")
    env["TELEGRAM_BRIEF_CHAT_ID"] = _ask(
        "TELEGRAM_BRIEF_CHAT_ID:",
        default=env.get("TELEGRAM_BRIEF_CHAT_ID", ""),
    )

    print()
    print("Optional: group chat support.")
    print("Add the bot to a Telegram group, then optionally restrict which")
    print("groups it'll respond in. Group chat IDs are negative integers;")
    print("find one by adding @RawDataBot to the target group, or watch the")
    print("daemon log on first run after the bot is added.")
    print("Leave empty to allow any group the bot is in (subject to the")
    print("user allowlist above).")
    env["TELEGRAM_ALLOWED_CHAT_IDS"] = _ask(
        "TELEGRAM_ALLOWED_CHAT_IDS (comma-sep, or empty for no filter):",
        default=env.get("TELEGRAM_ALLOWED_CHAT_IDS", ""),
    )

    print()
    print("Group trigger substrings (case-insensitive). The bot always")
    print("accepts @-mentions of its own username; this extends that list.")
    print("Defaults to '@agent, hey agent, agent,' if blank.")
    print("Note: Telegram bots default to 'privacy mode' — they only see")
    print("direct mentions in groups. To see all group messages, message")
    print("@BotFather → /setprivacy → Disable.")
    env["TELEGRAM_GROUP_TRIGGERS"] = _ask(
        "TELEGRAM_GROUP_TRIGGERS (comma-sep, or empty for defaults):",
        default=env.get("TELEGRAM_GROUP_TRIGGERS", ""),
    )

    print()
    print("Step 3: in Telegram, send /start to your bot from your phone so")
    print("        Telegram authorizes the bot to send messages back to you.")
    print()
    print("After install, you can verify with:")
    print("  python -m relay.telegram_relay --check")


def _step_discord_config(env: dict[str, str]) -> None:
    print()
    print("─ Discord config ─")
    print("Step 1: create the bot")
    print("  discord.com/developers/applications → New Application →")
    print("  name it 'personal_agent' → Bot tab → enable 'Message Content")
    print("  Intent' (privileged) → Reset Token → copy.")
    print()
    env["DISCORD_BOT_TOKEN"] = _ask(
        "DISCORD_BOT_TOKEN:", default=env.get("DISCORD_BOT_TOKEN", "")
    )

    print()
    print("Step 2: invite the bot to a server you admin")
    print("  OAuth2 → URL Generator → Scopes: bot. Bot Permissions:")
    print("  Send Messages, Read Message History, Attach Files. Open the")
    print("  generated URL, pick a server, authorize.")
    print()
    print("Step 3: find your Discord user id")
    print("  Settings → Advanced → Developer Mode on. Right-click yourself")
    print("  → Copy User ID. Comma-separated for multiple.")
    env["DISCORD_ALLOWED_USER_IDS"] = _ask(
        "DISCORD_ALLOWED_USER_IDS:",
        default=env.get("DISCORD_ALLOWED_USER_IDS", ""),
    )

    print()
    print("Optional: recipient id for scheduled briefs. Defaults to first allowed.")
    env["DISCORD_BRIEF_RECIPIENT_ID"] = _ask(
        "DISCORD_BRIEF_RECIPIENT_ID:",
        default=env.get("DISCORD_BRIEF_RECIPIENT_ID", ""),
    )

    print()
    print("Optional: server-channel support (in addition to DMs).")
    print("Set DISCORD_ALLOWED_CHANNEL_IDS to a comma-separated list of")
    print("channel IDs (Developer Mode → right-click channel → Copy ID).")
    print("The bot only responds in those channels when it's @-mentioned")
    print("or when a message contains a DISCORD_GROUP_TRIGGERS substring.")
    print("Leave empty for DM-only behavior.")
    env["DISCORD_ALLOWED_CHANNEL_IDS"] = _ask(
        "DISCORD_ALLOWED_CHANNEL_IDS (comma-sep, empty = DM-only):",
        default=env.get("DISCORD_ALLOWED_CHANNEL_IDS", ""),
    )
    if env["DISCORD_ALLOWED_CHANNEL_IDS"].strip():
        env["DISCORD_GROUP_TRIGGERS"] = _ask(
            "DISCORD_GROUP_TRIGGERS (comma-sep, empty = defaults):",
            default=env.get("DISCORD_GROUP_TRIGGERS", ""),
        )

    print()
    print("After install, verify with:  python -m relay.discord_relay --check")


def _step_slack_config(env: dict[str, str]) -> None:
    print()
    print("─ Slack config ─")
    print("Slack uses Socket Mode — Slack opens a WebSocket back to this")
    print("daemon, so there's no public webhook URL to configure.")
    print()
    print("Step 1: create the app")
    print("  api.slack.com/apps → Create New App → From scratch → name")
    print("  'personal_agent' → pick a workspace you control.")
    print()
    print("Step 2: enable Socket Mode")
    print("  Socket Mode → Enable → create an App-Level Token")
    print("  'personal_agent_socket' with scope `connections:write`. Copy")
    print("  the resulting xapp-… token.")
    env["SLACK_APP_TOKEN"] = _ask(
        "SLACK_APP_TOKEN:", default=env.get("SLACK_APP_TOKEN", "")
    )

    print()
    print("Step 3: bot token")
    print("  OAuth & Permissions → Bot Token Scopes → add:")
    print("    chat:write, im:history, im:read, files:read, users:read")
    print("  Install to Workspace. Copy the Bot User OAuth Token (xoxb-…).")
    env["SLACK_BOT_TOKEN"] = _ask(
        "SLACK_BOT_TOKEN:", default=env.get("SLACK_BOT_TOKEN", "")
    )

    print()
    print("Step 4: enable message.im event")
    print("  Event Subscriptions → Enable Events → Subscribe to bot")
    print("  events: message.im")
    print()
    print("Step 5: your Slack user id")
    print("  Workspace → click your name → ⋯ → 'Copy member ID' (Uxxx…).")
    print("  Comma-separated for multiple.")
    env["SLACK_ALLOWED_USER_IDS"] = _ask(
        "SLACK_ALLOWED_USER_IDS:",
        default=env.get("SLACK_ALLOWED_USER_IDS", ""),
    )

    print()
    print("Optional: user id for scheduled briefs. Defaults to first allowed.")
    env["SLACK_BRIEF_USER_ID"] = _ask(
        "SLACK_BRIEF_USER_ID:", default=env.get("SLACK_BRIEF_USER_ID", "")
    )

    print()
    print("Optional: channel / group / mpim support (in addition to DMs).")
    print("Set SLACK_ALLOWED_CHANNEL_IDS to a comma-separated list of")
    print("channel IDs (Cxxxxx for public, Gxxxxx for private). The bot")
    print("only responds when @-mentioned or matched by a trigger.")
    print("IMPORTANT: also add message.channels / message.groups /")
    print("message.mpim to the app's Event Subscriptions, otherwise the")
    print("bot can't see channel messages. Leave empty for DM-only.")
    env["SLACK_ALLOWED_CHANNEL_IDS"] = _ask(
        "SLACK_ALLOWED_CHANNEL_IDS (comma-sep, empty = DM-only):",
        default=env.get("SLACK_ALLOWED_CHANNEL_IDS", ""),
    )
    if env["SLACK_ALLOWED_CHANNEL_IDS"].strip():
        env["SLACK_GROUP_TRIGGERS"] = _ask(
            "SLACK_GROUP_TRIGGERS (comma-sep, empty = defaults):",
            default=env.get("SLACK_GROUP_TRIGGERS", ""),
        )

    print()
    print("After install, verify with:  python -m relay.slack_relay --check")


def _step_sms_config(env: dict[str, str]) -> None:
    print()
    print("─ SMS via Twilio config ─")
    print("Fifth transport — bidirectional SMS via Twilio. Text-only")
    print("(no image attachments / vision flow on inbound).")
    print()
    print("Cost: ~$1/mo for the phone number + ~$0.008 per message.")
    print()
    print("Step 1: twilio.com → sign up, verify, buy an SMS-capable")
    print("phone number. Then Console → Account → API keys & tokens —")
    print("copy the Account SID and the Auth Token.")
    env["TWILIO_ACCOUNT_SID"] = _ask(
        "TWILIO_ACCOUNT_SID:", default=env.get("TWILIO_ACCOUNT_SID", "")
    )
    env["TWILIO_AUTH_TOKEN"] = _ask(
        "TWILIO_AUTH_TOKEN:", default=env.get("TWILIO_AUTH_TOKEN", "")
    )

    print()
    print("Step 2: your Twilio phone number in E.164 (+15551234567).")
    env["TWILIO_FROM_NUMBER"] = _ask(
        "TWILIO_FROM_NUMBER:", default=env.get("TWILIO_FROM_NUMBER", "")
    )

    print()
    print("Step 3: phone numbers that are allowed to text the bot.")
    print("Comma-separated, E.164 (+15551234567). The relay drops every")
    print("other inbound message — set this to at least your own phone.")
    env["SMS_ALLOWED_NUMBERS"] = _ask(
        "SMS_ALLOWED_NUMBERS:", default=env.get("SMS_ALLOWED_NUMBERS", "")
    )

    print()
    print("Optional: webhook port (default 8781). Bound to 127.0.0.1.")
    env["SMS_WEBHOOK_PORT"] = _ask(
        "SMS_WEBHOOK_PORT:", default=env.get("SMS_WEBHOOK_PORT", "8781")
    )

    print()
    print("Optional: recipient for scheduled briefs / reminders.")
    print("Defaults to the first number in SMS_ALLOWED_NUMBERS.")
    env["SMS_BRIEF_RECIPIENT"] = _ask(
        "SMS_BRIEF_RECIPIENT:", default=env.get("SMS_BRIEF_RECIPIENT", "")
    )

    print()
    print("Step 4 (USER ACTION REQUIRED AFTER INSTALL):")
    port = env.get("SMS_WEBHOOK_PORT", "8781")
    print(f"  Twilio needs a public URL to deliver inbound messages to.")
    print(f"  In a separate terminal:  ngrok http {port}")
    print(f"  Copy the https://...ngrok-free.app URL. Then in the Twilio")
    print(f"  Console → Phone Numbers → your number → Messaging:")
    print(f"  set 'A MESSAGE COMES IN' to <ngrok-url>/sms/webhook (POST).")
    print(f"  Save. Text your Twilio number from one of the allowed phones.")
    print()
    print("After install, verify with:  python -m relay.sms_relay --check")


# ─── Triggers (email watch + scheduler config) ─────────────────────────────


def _load_triggers_config() -> dict[str, Any]:
    """Load the live triggers.yaml if it exists, else fall back to the
    committed template. Returns a dict that gets mutated by prompts and
    written back via _write_triggers_yaml.
    """
    import yaml

    if TRIGGERS_PATH.exists():
        return yaml.safe_load(TRIGGERS_PATH.read_text()) or {}
    if TRIGGERS_EXAMPLE_PATH.exists():
        return yaml.safe_load(TRIGGERS_EXAMPLE_PATH.read_text()) or {}
    return {}


def _write_triggers_yaml(cfg: dict[str, Any]) -> None:
    """Hand-formatted YAML emitter that preserves the file's section
    structure + key comments. yaml.safe_dump strips comments and can
    re-order keys, so we write it ourselves for readability.
    """
    et = cfg.get("email_triggers") or {}
    sched = cfg.get("scheduled") or {}
    overdue = cfg.get("overdue_check") or {}
    mb = sched.get("morning_brief") or {}
    wr = sched.get("weekly_review") or {}

    llm_cfg = et.get("llm_classification") or {}
    max_per_check = int(llm_cfg.get("max_per_check", 30))
    ea = cfg.get("expected_arrivals") or {}
    ea_watches = list(ea.get("watches") or [])

    mb_include = list(mb.get("include") or [])
    wr_include = list(wr.get("include") or [])

    out: list[str] = []
    out.append("# Trigger configuration — generated by tools/install.py.")
    out.append("# Edit by hand or re-run install.py to reconfigure.")
    out.append("# Re-loaded by the scheduler on every tick (~30s).")
    out.append("# This file is gitignored; the committed template is")
    out.append("# config/triggers.yaml.example.")
    out.append("")
    out.append("# Real-time email triage. Every non-automated unread email")
    out.append("# gets one Haiku 4.5 triage call that decides flag + emits")
    out.append("# action-shaped ping items. No allowlist; no keyword list.")
    out.append("email_triggers:")
    out.append(f"  enabled: {str(bool(et.get('enabled', False))).lower()}")
    out.append(f"  every_minutes: {int(et.get('every_minutes', 15))}")
    out.append("  llm_classification:")
    out.append(f"    max_per_check: {max_per_check}")
    out.append("")
    out.append("expected_arrivals:")
    out.append(f"  enabled: {str(bool(ea.get('enabled', False))).lower()}")
    out.append(f"  cadence_hours: {int(ea.get('cadence_hours', 12))}")
    out.append("  watches:")
    if ea_watches:
        for w in ea_watches:
            out.append(f"    - name: \"{w.get('name', '')}\"")
            out.append(f"      event_date: \"{w.get('event_date', '')}\"")
            out.append(f"      expected_sender: \"{w.get('expected_sender', '')}\"")
            if w.get("sender_label"):
                out.append(f"      sender_label: \"{w['sender_label']}\"")
            out.append(f"      expected_subject: \"{w.get('expected_subject', '')}\"")
            out.append(f"      lead_time_days: {int(w.get('lead_time_days', 7))}")
    else:
        out.append("    []")
    out.append("")
    out.append("scheduled:")
    out.append("  morning_brief:")
    out.append(f"    enabled: {str(bool(mb.get('enabled', True))).lower()}")
    out.append(f'    time: "{mb.get("time", "07:30")}"')
    out.append(f"    weekdays_only: {str(bool(mb.get('weekdays_only', False))).lower()}")
    out.append("    include:")
    for item in (mb_include or ["todays_calendar", "top_tasks", "urgent_unread_emails"]):
        out.append(f"      - {item}")
    out.append("  weekly_review:")
    out.append(f"    enabled: {str(bool(wr.get('enabled', True))).lower()}")
    out.append(f"    day: {wr.get('day', 'sunday')}")
    out.append(f'    time: "{wr.get("time", "20:00")}"')
    out.append("    include:")
    for item in (wr_include or ["incomplete_tasks_last_week", "upcoming_week_calendar"]):
        out.append(f"      - {item}")
    out.append("")
    out.append("overdue_check:")
    out.append(f"  enabled: {str(bool(overdue.get('enabled', False))).lower()}")
    out.append(f"  cadence_minutes: {int(overdue.get('cadence_minutes', 60))}")
    out.append(f"  min_priority: {int(overdue.get('min_priority', 3))}")

    TRIGGERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRIGGERS_PATH.write_text("\n".join(out) + "\n")
    os.chmod(TRIGGERS_PATH, 0o600)


def _split_csv_or_lines(raw: str) -> list[str]:
    """Accept either comma-separated or one-per-line input."""
    items: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        s = chunk.strip()
        if s:
            items.append(s)
    return items


def step_triggers(env: dict[str, str], enabled: set[str]) -> None:
    _hr("X. Email-watch + scheduler triggers")

    cfg = _load_triggers_config()
    et = cfg.setdefault("email_triggers", {})

    if "gmail" not in enabled:
        print("Skipping email-watch — gmail sub-agent isn't enabled (it's the source).")
        print("(Other scheduler triggers — morning brief, weekly review — still configured)")
        # Still write through the loaded config so triggers.yaml exists
        # for the scheduler. Email watch stays disabled.
        et.setdefault("enabled", False)
        _write_triggers_yaml(cfg)
        _ok(f"wrote {TRIGGERS_PATH}")
        return

    print("Email watch runs every non-automated unread email through one")
    print("Haiku 4.5 triage call. Haiku decides whether to ping you AND")
    print("writes the action-shaped blurb (logistics, date/time, what-to-")
    print("bring, decision needed). No allowlist; no keyword list.")
    print()
    print("Typical cost: $0.10-$1/day depending on inbox volume.")
    print()

    enable = _yn(
        "Enable email watch?",
        default=bool(et.get("enabled", False)),
    )
    et["enabled"] = enable

    if enable:
        every_default = str(et.get("every_minutes", 15))
        every_raw = _ask(
            "How often to check (minutes, 5-60)?", default=every_default
        )
        try:
            et["every_minutes"] = max(1, int(every_raw))
        except ValueError:
            _warn(f"keeping {every_default}")

        llm_cfg = et.setdefault("llm_classification", {})
        max_default = str(llm_cfg.get("max_per_check", 30))
        max_raw = _ask(
            "Max Haiku triage calls per fire?", default=max_default
        )
        try:
            llm_cfg["max_per_check"] = max(1, int(max_raw))
        except ValueError:
            _warn(f"keeping {max_default}")

    # Drop the legacy keys quietly — they're ignored by the scheduler
    # now, but leaving them in the rendered yaml is confusing.
    et.pop("important_senders", None)
    et.pop("urgency_keywords", None)

    _write_triggers_yaml(cfg)
    _ok(f"wrote {TRIGGERS_PATH}")
    if enable:
        cap = (et.get("llm_classification") or {}).get("max_per_check", 30)
        print(
            f"  Email watch active: every {et.get('every_minutes', 15)} "
            f"min, up to {cap} Haiku calls per fire"
        )
    else:
        print("  Email watch disabled — set enabled=true in triggers.yaml when ready")


# ─── Step 5 — Behavior defaults ─────────────────────────────────────────────


def step_behavior(env: dict[str, str]) -> None:
    _hr("5. Behavior defaults")
    env["USER_TIMEZONE"] = _ask(
        "USER_TIMEZONE (IANA, e.g. America/Chicago):",
        default=env.get("USER_TIMEZONE", "America/Chicago"),
    )
    env["CLAUDE_MODEL"] = _ask(
        "CLAUDE_MODEL:",
        default=env.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
    )


# ─── Step 6 — LaunchAgents ──────────────────────────────────────────────────


def step_launchagents() -> None:
    _hr("6. LaunchAgents (auto-start on login)")
    print("Four launch agents: relay, scheduler, log-rotation, webui.")
    print("Installing renders absolute paths into the plists, copies them")
    print("to ~/Library/LaunchAgents/, and starts them via launchctl.")
    print("The webui daemon serves the local admin UI at http://127.0.0.1:8780.")
    if not _yn("Install LaunchAgents now?", default=False):
        print("  Skipped — run ./launch_agents/install.sh later when ready.")
        return
    try:
        subprocess.run(
            ["./launch_agents/install.sh"],
            cwd=str(V1_DIR),
            check=True,
        )
    except subprocess.CalledProcessError as e:
        _err(f"LaunchAgent install failed: {e}")


# ─── Final summary ──────────────────────────────────────────────────────────


def step_summary(env: dict[str, str], enabled: set[str]) -> None:
    _hr("Done")
    print(f"Config written to {ENV_PATH}")
    print(f"Enabled sub-agents ({len(enabled)}): {', '.join(sorted(enabled))}")
    print()
    print("Web admin UI (if LaunchAgents installed):")
    print("  http://127.0.0.1:8780")
    print("  Dashboard + chat + history + observability + config editors.")
    print()
    print("Next steps:")
    print("  • Verify token health:     python -m tools.token_health")
    print("  • Test the agent in REPL:  python agent_host.py")
    print("  • Run the relay manually:  python -m relay.imessage_relay --check")
    print("  • Cost report:             python -m tools.cost_report")
    print()
    print("To re-run this configurator (add a sub-agent, fix a key, etc.):")
    print("  ./install.sh --skip-deps")


# ─── Main ───────────────────────────────────────────────────────────────────


def step_disclosure(env: dict[str, str]) -> None:
    """Plain-English privacy disclosure at the top of the install run.

    Gives a fork-and-run user a chance to back out before the
    configurator writes anything. Skipped silently when the .env
    already has ANTHROPIC_API_KEY set (re-runs are common; we don't
    want to nag returning users)."""
    if env.get("ANTHROPIC_API_KEY", "").strip():
        return  # not first-run; user has already seen this

    print()
    _hr("Before you start — privacy & security disclosure")
    print()
    print("This installer configures a personal AI agent that:")
    print()
    print("  • Stores every conversation, fact, and Claude API event")
    print("    on your machine in plaintext SQLite under v1/data/.")
    print("  • Sends every message you exchange AND every email body")
    print("    the scheduler triages to Anthropic's API for processing.")
    print("  • Holds OAuth refresh tokens for Gmail, Calendar, Spotify,")
    print("    and every other connected sub-agent in plaintext files")
    print("    under v1/data/. Stolen tokens give a third party ~")
    print("    indefinite access to those services.")
    print("  • Exposes a local-only web UI at http://127.0.0.1:8780.")
    print("    No authentication; no CSRF protection. Treat it as you")
    print("    would a local database port.")
    print()
    print("This is a single-user, local-first tool. Don't run it on a")
    print("shared machine. Don't sync v1/ to iCloud Drive, Dropbox, or")
    print("Time Machine without thinking through who can access those.")
    print()
    print("Read README.md's 'Privacy & security profile' section before")
    print("continuing. Threat models the agent does and does NOT defend")
    print("against are spelled out there.")
    print()
    if not _yn("Acknowledge and continue?", default=False):
        print("\nbailing out — nothing was written.")
        sys.exit(0)
    print()


def main() -> None:
    print()
    print("personal_agent — interactive configurator")
    print()
    print("Existing values are preserved unless you change them. Empty input")
    print("at any prompt means 'keep current'. Re-run anytime to add or")
    print("change sub-agents.")
    print()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    env = _read_env()

    step_disclosure(env)
    step_migration(env)
    step_required(env)
    enabled = step_subagents(env)
    step_google_oauth(env, enabled)
    step_relay(env)
    step_triggers(env, enabled)
    step_behavior(env)
    _write_env(env)
    print()
    _ok(f"wrote {ENV_PATH}")
    step_launchagents()
    print()
    step_summary(env, enabled)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n\ninterrupted — partial config may have been saved to .env")
        sys.exit(130)
