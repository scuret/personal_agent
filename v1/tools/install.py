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
  6. LaunchAgents — offer to install all three (relay, scheduler,
     log-rotation) so the daemons auto-start on login.

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


# ─── Sub-agent registry ─────────────────────────────────────────────────────


@dataclass
class SubAgent:
    name: str
    description: str
    env_vars: list[str] = field(default_factory=list)
    needs_google_oauth: bool = False
    auth_help: str = ""           # one-liner shown when prompting
    setup_url: str = ""            # link to where the user gets the key
    always_on: bool = False        # no auth → can't be turned off


SUBAGENTS: list[SubAgent] = [
    SubAgent("memory", "Conversation archive + extracted facts + audit log", always_on=True),
    SubAgent("weather", "Open-Meteo current + forecast", always_on=True),
    SubAgent("vision", "Image analysis on iMessage attachments", always_on=True),
    SubAgent("wikipedia", "Search + read articles", always_on=True),
    SubAgent("reddit", "Public-read subreddits / search / posts", always_on=True),
    SubAgent("reminders", "Schedule 'remind me at 4pm' iMessage pings", always_on=True),
    SubAgent(
        "todoist",
        "Task management",
        env_vars=["TODOIST_API_KEY"],
        setup_url="https://todoist.com/app/settings/integrations/developer",
    ),
    SubAgent(
        "gmail",
        "Read, search, draft, archive (NEVER sends)",
        needs_google_oauth=True,
        setup_url="https://console.cloud.google.com (enable Gmail API + create Desktop OAuth client)",
    ),
    SubAgent(
        "calendar",
        "Read events, search, free/busy",
        needs_google_oauth=True,
        setup_url="https://console.cloud.google.com (enable Calendar API)",
    ),
    SubAgent(
        "notion",
        "Search, read pages, query DBs, create + append",
        env_vars=["NOTION_INTEGRATION_TOKEN"],
        setup_url="https://www.notion.so/profile/integrations",
        auth_help="Internal integration token. Also share each page/db with the integration.",
    ),
    SubAgent(
        "github",
        "Repos, issues, PRs, search, create issue",
        env_vars=["GITHUB_TOKEN"],
        setup_url="https://github.com/settings/tokens",
        auth_help="Classic with `repo` scope OR fine-grained with Issues r+w / PRs r / Contents r / Metadata r.",
    ),
    SubAgent(
        "web",
        "Brave Search + URL fetch",
        env_vars=["BRAVE_SEARCH_API_KEY"],
        setup_url="https://api.search.brave.com",
        auth_help="Subscribe → Free tier (2K queries/month). Requires a credit card on file.",
    ),
    SubAgent(
        "youtube",
        "Search + video/channel metadata (public read)",
        env_vars=["YOUTUBE_API_KEY"],
        setup_url="https://console.cloud.google.com (enable YouTube Data API v3 → API key)",
        auth_help="Just an API key, NOT OAuth. Free quota: 10K units/day.",
    ),
    SubAgent(
        "dropbox",
        "Search, list, read text, share-link",
        env_vars=["DROPBOX_ACCESS_TOKEN"],
        setup_url="https://www.dropbox.com/developers/apps",
        auth_help="Scoped Access app. Permissions: files.metadata.read, files.content.read, sharing.read.",
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
    print()

    current = env.get("RELAY_TRANSPORT", "imessage")
    transport = _ask("RELAY_TRANSPORT [imessage/telegram]:", default=current)
    if transport not in ("imessage", "telegram"):
        _warn(f"unknown transport {transport!r}, keeping {current}")
        transport = current
    env["RELAY_TRANSPORT"] = transport

    if transport == "imessage":
        _step_imessage_config(env)
    else:
        _step_telegram_config(env)


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
    print("Step 3: in Telegram, send /start to your bot from your phone so")
    print("        Telegram authorizes the bot to send messages back to you.")
    print()
    print("After install, you can verify with:")
    print("  python -m relay.telegram_relay --check")


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
    print("Three launch agents: relay, scheduler, log-rotation.")
    print("Installing renders absolute paths into the plists, copies them")
    print("to ~/Library/LaunchAgents/, and starts them via launchctl.")
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
    print("Next steps:")
    print("  • Verify token health:     python -m tools.token_health")
    print("  • Test the agent in REPL:  python agent_host.py")
    print("  • Run the relay manually:  python -m relay.imessage_relay --check")
    print("  • Cost report:             python -m tools.cost_report")
    print()
    print("To re-run this configurator (add a sub-agent, fix a key, etc.):")
    print("  ./install.sh --skip-deps")


# ─── Main ───────────────────────────────────────────────────────────────────


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

    step_migration(env)
    step_required(env)
    enabled = step_subagents(env)
    step_google_oauth(env, enabled)
    step_relay(env)
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
