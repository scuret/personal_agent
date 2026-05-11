"""Interactive uninstaller for personal_agent.

Removes the app — fully or by sub-agent — and the surrounding runtime
state (LaunchAgents, cached tokens, local data, venv). Mirrors the
shape of tools/install.py and reuses its SUBAGENTS registry so the two
stay in sync.

Run modes:

  python -m tools.uninstall                       # interactive menu
  python -m tools.uninstall --all                  # full uninstall (prompts before each step)
  python -m tools.uninstall --all --yes           # full, no prompts (dangerous)
  python -m tools.uninstall --sub-agent <name>    # remove one sub-agent
  python -m tools.uninstall --sub-agent a,b,c     # remove several
  python -m tools.uninstall --launchagents        # stop + remove daemons only
  python -m tools.uninstall --data                # wipe local data (sqlite + logs + caches)
  python -m tools.uninstall --list                # show what's currently installed
  python -m tools.uninstall --dry-run [...]       # preview without doing

Sub-agent removal:
  • Clears the sub-agent's env vars in .env (replaces values with empty
    strings; comments + structure preserved).
  • Deletes the sub-agent's cached token file(s) if any.
  • Google family (gmail, calendar, drive, docs, sheets) shares one
    OAuth pickle — it's only deleted when the LAST remaining Google
    sub-agent is removed.
  • Tells you the URL to revoke the app at the provider's end (we can't
    revoke from here; the local token is gone, but the issued tokens
    technically remain valid until you revoke at the provider).

Full uninstall removes:
  • LaunchAgents (relay, scheduler, log-rotation) — bootout + plist delete
  • data/ contents — sqlite, logs, all token caches
  • .venv/
  • .env
  • config/credentials.json (Google OAuth client) + config/triggers.yaml

What we never touch:
  • The source code under v1/ — you remove that yourself with `rm -rf`
  • Provider-side tokens — revoke those at each service's app dashboard
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Reuse the sub-agent registry so install + uninstall stay in lockstep.
from tools.install import SUBAGENTS, SubAgent  # noqa: E402

V1_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = V1_DIR / ".env"
DATA_DIR = V1_DIR / "data"
CONFIG_DIR = V1_DIR / "config"
VENV_DIR = V1_DIR / ".venv"
LAUNCH_AGENTS_SCRIPT = V1_DIR / "launch_agents" / "uninstall.sh"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCH_AGENT_LABELS = [
    "com.personal-agent.relay",
    "com.personal-agent.scheduler",
    "com.personal-agent.log-rotation",
]

# Per-sub-agent token-cache file(s) (relative to V1_DIR). For Google
# sub-agents, the pickle is shared — see _google_subagents() / the
# remove logic for the "delete only when last Google removed" guard.
SUBAGENT_TOKEN_FILES: dict[str, list[str]] = {
    "dropbox": ["data/dropbox_token.json"],
    "spotify": ["data/spotify_token.json"],
    "canva":   ["data/canva_token.json"],
    "linkedin": ["data/linkedin_token.json"],
    # Google family — shared pickle, handled specially
    "gmail":    ["data/google_token.pickle"],
    "calendar": ["data/google_token.pickle"],
    "drive":    ["data/google_token.pickle"],
    "docs":     ["data/google_token.pickle"],
    "sheets":   ["data/google_token.pickle"],
}

# Where to revoke the app at the provider's end after a local uninstall.
# We can't do this for the user — local token deletion ≠ provider-side
# revocation; the issued tokens may stay valid until they're revoked
# at the provider's app dashboard.
SUBAGENT_REVOCATION_URLS: dict[str, str] = {
    "dropbox":  "https://www.dropbox.com/account/connected_apps",
    "spotify":  "https://www.spotify.com/account/apps/",
    "canva":    "https://www.canva.com/settings/your-apps",
    "linkedin": "https://www.linkedin.com/psettings/permitted-services",
    "gmail":    "https://myaccount.google.com/permissions",
    "calendar": "https://myaccount.google.com/permissions",
    "drive":    "https://myaccount.google.com/permissions",
    "docs":     "https://myaccount.google.com/permissions",
    "sheets":   "https://myaccount.google.com/permissions",
    "todoist":  "https://app.todoist.com/app/settings/integrations/developer",
    "notion":   "https://www.notion.so/profile/integrations",
    "github":   "https://github.com/settings/tokens",
    "web":      "https://api.search.brave.com",
    "youtube":  "https://console.cloud.google.com",
}

GOOGLE_FAMILY = {"gmail", "calendar", "drive", "docs", "sheets"}


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


def _info(msg: str) -> None:
    print(f"    {msg}")


def _yn(prompt: str, default: bool = False) -> bool:
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


def _clear_env_keys(keys: list[str], dry_run: bool) -> int:
    """Replace each key's value in .env with empty string. Preserves
    file structure (comments, ordering). Returns number of keys cleared."""
    if not ENV_PATH.exists():
        return 0
    text = ENV_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    cleared = 0
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.+)$", line)
        if m and m.group(1) in keys:
            if dry_run:
                _info(f"would clear: {m.group(1)}=")
            else:
                lines[i] = f"{m.group(1)}="
            cleared += 1
    if cleared and not dry_run:
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(ENV_PATH, 0o600)
    return cleared


def _enabled_sub_agents(env: dict[str, str]) -> set[str]:
    """Return the names of sub-agents currently configured (creds present).

    `always_on` sub-agents aren't included here — they have no env var
    to clear, so they don't show up in install/uninstall listings.
    """
    enabled: set[str] = set()
    for sa in SUBAGENTS:
        if sa.always_on:
            continue
        # Sub-agent counts as "enabled" if all its env vars are populated.
        if sa.env_vars and all(env.get(v, "").strip() for v in sa.env_vars):
            enabled.add(sa.name)
        elif sa.needs_google_oauth:
            # Google-OAuth sub-agents are "enabled" when credentials.json exists.
            if (CONFIG_DIR / "credentials.json").exists():
                enabled.add(sa.name)
    return enabled


# ─── Per-sub-agent removal ──────────────────────────────────────────────────


def _find_sub_agent(name: str) -> SubAgent | None:
    return next((sa for sa in SUBAGENTS if sa.name == name), None)


def remove_sub_agent(name: str, env: dict[str, str], dry_run: bool) -> bool:
    """Remove one sub-agent's runtime footprint. Returns True if anything
    was actually removed."""
    sa = _find_sub_agent(name)
    if sa is None:
        _err(f"unknown sub-agent: {name}")
        return False
    if sa.always_on:
        _warn(f"{name} is always-on (no auth, no cached state) — nothing to remove.")
        return False

    _hr(f"Removing sub-agent: {name}")
    print(f"  {sa.description}")
    print()

    changed = False
    held_by_guard = False

    # 1. Clear env vars.
    if sa.env_vars:
        n = _clear_env_keys(sa.env_vars, dry_run)
        if n:
            verb = "would clear" if dry_run else "cleared"
            _ok(f"{verb} {n} env var(s) in .env: {', '.join(sa.env_vars)}")
            changed = True
        else:
            _info(f"env vars already empty: {', '.join(sa.env_vars)}")

    # 2. Delete cached token file(s) — with Google-family guard.
    token_files = SUBAGENT_TOKEN_FILES.get(name, [])
    for relpath in token_files:
        full = V1_DIR / relpath
        if not full.exists():
            _info(f"no cached token at {relpath}")
            continue

        # Guard: the Google pickle is shared across gmail/calendar/drive/docs/sheets.
        # Only delete it when we're removing the LAST Google sub-agent.
        if name in GOOGLE_FAMILY:
            remaining_google = (_enabled_sub_agents(env) & GOOGLE_FAMILY) - {name}
            if remaining_google:
                _warn(
                    f"keeping {relpath} — still used by: "
                    f"{', '.join(sorted(remaining_google))}"
                )
                held_by_guard = True
                continue

        if dry_run:
            _info(f"would delete: {relpath}")
        else:
            full.unlink()
            _ok(f"deleted: {relpath}")
        changed = True

    # 3. Google bundle warning: removing one Google sub-agent doesn't
    # actually unregister it — agent_host.py registers all Google
    # sub-agents whenever credentials.json is present. The only way
    # to fully remove an individual one is to remove them all (which
    # deletes the shared pickle + credentials.json on the next data
    # cleanup), or to manually edit agent_host.py to exclude it.
    if name in GOOGLE_FAMILY and held_by_guard:
        print()
        _warn(
            "Google sub-agents share OAuth — removing this one "
            "individually does NOT unregister it from agent_host."
        )
        _info(
            "to fully disable, remove all Google sub-agents in one call:"
        )
        _info(
            "  python -m tools.uninstall --sub-agent "
            + ",".join(sorted(GOOGLE_FAMILY))
        )

    # 4. Revocation reminder.
    rev_url = SUBAGENT_REVOCATION_URLS.get(name)
    if rev_url and changed:
        print()
        _warn(
            "local state removed, but the app may still be authorized "
            "at the provider."
        )
        _info(f"revoke at: {rev_url}")

    if not changed and not held_by_guard:
        _info("(nothing to remove — sub-agent wasn't installed.)")
    return changed


# ─── LaunchAgents ───────────────────────────────────────────────────────────


def _launchagents_installed() -> list[str]:
    """Return the subset of LaunchAgents currently installed (plist on disk)."""
    return [label for label in LAUNCH_AGENT_LABELS
            if (LAUNCH_AGENTS_DIR / f"{label}.plist").exists()]


def remove_launchagents(dry_run: bool) -> bool:
    _hr("Removing LaunchAgents")
    installed = _launchagents_installed()
    if not installed:
        _info("no LaunchAgents installed.")
        return False
    for label in installed:
        plist = LAUNCH_AGENTS_DIR / f"{label}.plist"
        if dry_run:
            _info(f"would bootout + remove: {label}  ({plist})")
            continue
    if dry_run:
        return True

    # Delegate to the existing bash helper — it handles bootout correctly,
    # including the "not currently loaded" case.
    if not LAUNCH_AGENTS_SCRIPT.exists():
        _err(f"launch_agents/uninstall.sh not found at {LAUNCH_AGENTS_SCRIPT}")
        return False
    try:
        subprocess.run([str(LAUNCH_AGENTS_SCRIPT)], cwd=str(V1_DIR), check=True)
    except subprocess.CalledProcessError as e:
        _err(f"launch_agents/uninstall.sh failed: {e}")
        return False
    _ok(f"removed {len(installed)} LaunchAgent(s)")
    return True


# ─── Local data ─────────────────────────────────────────────────────────────


def _data_files() -> list[Path]:
    """Files under data/ that we'd remove. Always-keep: .gitkeep if any."""
    if not DATA_DIR.exists():
        return []
    out: list[Path] = []
    for p in DATA_DIR.iterdir():
        if p.name.startswith("."):
            continue
        out.append(p)
    return out


def remove_data(dry_run: bool) -> bool:
    _hr("Removing local data")
    files = _data_files()
    if not files:
        _info("data/ is already empty.")
        return False
    print(f"  data/ contains {len(files)} item(s):")
    for p in files:
        size = ""
        try:
            if p.is_file():
                size = f"  ({p.stat().st_size:,} bytes)"
            elif p.is_dir():
                size = "  (dir)"
        except OSError:
            pass
        print(f"    - {p.name}{size}")
    if dry_run:
        _info("(dry-run; nothing deleted)")
        return True
    for p in files:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError as e:
            _err(f"failed to remove {p}: {e}")
            continue
    _ok(f"removed {len(files)} item(s) from data/")
    return True


# ─── Venv + .env + config ──────────────────────────────────────────────────


def remove_venv(dry_run: bool) -> bool:
    _hr("Removing virtualenv")
    if not VENV_DIR.exists():
        _info("no .venv/ to remove.")
        return False
    if dry_run:
        _info(f"would delete: {VENV_DIR}")
        return True
    try:
        shutil.rmtree(VENV_DIR)
    except OSError as e:
        _err(f"failed to remove .venv/: {e}")
        return False
    _ok("removed .venv/")
    return True


def remove_env(dry_run: bool) -> bool:
    _hr("Removing .env")
    if not ENV_PATH.exists():
        _info(".env doesn't exist.")
        return False
    if dry_run:
        _info(f"would delete: {ENV_PATH}")
        return True
    ENV_PATH.unlink()
    _ok("removed .env")
    return True


def remove_config_secrets(dry_run: bool) -> bool:
    """Remove config/credentials.json + config/triggers.yaml — the
    user-specific config that has secrets or personal data. Leaves the
    .example templates in place so future installs still have them."""
    _hr("Removing config secrets")
    removed_any = False
    for relpath in ("config/credentials.json", "config/triggers.yaml"):
        p = V1_DIR / relpath
        if not p.exists():
            _info(f"no {relpath}")
            continue
        if dry_run:
            _info(f"would delete: {relpath}")
            removed_any = True
            continue
        p.unlink()
        _ok(f"removed: {relpath}")
        removed_any = True
    return removed_any


# ─── Full + listing ─────────────────────────────────────────────────────────


def full_uninstall(dry_run: bool, yes: bool) -> None:
    _hr("FULL UNINSTALL")
    print("This will remove:")
    print("  • LaunchAgents (relay, scheduler, log-rotation)")
    print("  • data/ contents (sqlite, logs, all token caches)")
    print("  • .venv/")
    print("  • .env (your API keys)")
    print("  • config/credentials.json + config/triggers.yaml")
    print()
    print("It will NOT remove:")
    print("  • The source code under v1/ — `rm -rf v1/` yourself if you want")
    print("  • Provider-side authorizations — revoke at each service's app dashboard")
    print()
    if dry_run:
        print("(dry-run mode — nothing will actually be removed)")
        print()

    if not yes and not dry_run:
        if not _yn("Proceed with full uninstall?", default=False):
            print("aborted.")
            return

    remove_launchagents(dry_run)
    remove_data(dry_run)
    remove_venv(dry_run)
    remove_env(dry_run)
    remove_config_secrets(dry_run)

    print()
    _hr("Done" + (" (dry-run)" if dry_run else ""))
    if not dry_run:
        print("  Source code remains at:", V1_DIR)
        print("  To delete it: rm -rf", V1_DIR)
        print()
        print("  Don't forget to revoke provider-side authorizations:")
        for name, url in sorted(SUBAGENT_REVOCATION_URLS.items()):
            print(f"    • {name}: {url}")


def list_installed() -> None:
    _hr("Currently installed")
    env = _read_env()
    enabled = _enabled_sub_agents(env)

    always_on = sorted(sa.name for sa in SUBAGENTS if sa.always_on)
    optional_on = sorted(enabled)
    optional_off = sorted(
        sa.name for sa in SUBAGENTS
        if not sa.always_on and sa.name not in enabled
    )

    print(f"  always-on    ({len(always_on)}): {', '.join(always_on)}")
    print(f"  configured   ({len(optional_on)}): {', '.join(optional_on) or '(none)'}")
    print(f"  not enabled  ({len(optional_off)}): {', '.join(optional_off) or '(none)'}")
    print()

    la_installed = _launchagents_installed()
    print(f"  LaunchAgents ({len(la_installed)}/3 installed):"
          f" {', '.join(la_installed) or '(none)'}")
    print(f"  .venv/       : {'present' if VENV_DIR.exists() else 'absent'}")
    print(f"  .env         : {'present' if ENV_PATH.exists() else 'absent'}")
    print(f"  data/ items  : {len(_data_files())}")


# ─── Interactive menu ───────────────────────────────────────────────────────


def interactive(dry_run: bool) -> None:
    env = _read_env()
    while True:
        print()
        _hr("Uninstall menu")
        print("  1. Remove a specific sub-agent")
        print("  2. Remove LaunchAgents only")
        print("  3. Remove local data only (sqlite, logs, token caches)")
        print("  4. FULL uninstall (LaunchAgents + venv + data + .env + secrets)")
        print("  5. List what's currently installed")
        print("  6. Exit")
        choice = input("> ").strip()
        if choice == "1":
            enabled = sorted(_enabled_sub_agents(env))
            if not enabled:
                _info("no configured sub-agents to remove.")
                continue
            print("Configured sub-agents:")
            for i, name in enumerate(enabled, 1):
                print(f"  {i}. {name}")
            raw = input("Enter name (or number, or comma-separated list): ").strip()
            if not raw:
                continue
            picks: list[str] = []
            for token in raw.split(","):
                token = token.strip()
                if token.isdigit():
                    idx = int(token) - 1
                    if 0 <= idx < len(enabled):
                        picks.append(enabled[idx])
                elif token in enabled:
                    picks.append(token)
                else:
                    _warn(f"unknown / not-installed: {token}")
            for name in picks:
                remove_sub_agent(name, env, dry_run)
                env = _read_env()  # re-read after the env edit
            # Bundle cleanup if user removed the full Google family at once.
            if GOOGLE_FAMILY.issubset(set(picks)):
                _hr("Removing shared Google OAuth state")
                for relpath in ("data/google_token.pickle", "config/credentials.json"):
                    p = V1_DIR / relpath
                    if not p.exists():
                        _info(f"no {relpath}")
                        continue
                    if dry_run:
                        _info(f"would delete: {relpath}")
                    else:
                        p.unlink()
                        _ok(f"deleted: {relpath}")
        elif choice == "2":
            remove_launchagents(dry_run)
        elif choice == "3":
            if _yn("Remove all local data?", default=False):
                remove_data(dry_run)
        elif choice == "4":
            full_uninstall(dry_run, yes=False)
            return
        elif choice == "5":
            list_installed()
        elif choice == "6":
            return
        else:
            _warn("unknown choice.")


# ─── Entry point ────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="tools.uninstall",
        description="Uninstall personal_agent — fully or by sub-agent.",
    )
    parser.add_argument("--all", action="store_true",
                        help="full uninstall (LaunchAgents + venv + data + .env + secrets)")
    parser.add_argument("--sub-agent",
                        help="remove one or more sub-agents (comma-separated)")
    parser.add_argument("--launchagents", action="store_true",
                        help="remove LaunchAgents only")
    parser.add_argument("--data", action="store_true",
                        help="wipe local data (sqlite, logs, token caches)")
    parser.add_argument("--list", action="store_true",
                        help="show what's currently installed")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview without removing anything")
    parser.add_argument("--yes", action="store_true",
                        help="skip confirmation prompts (use with --all)")
    args = parser.parse_args()

    if args.list:
        list_installed()
        return 0

    if args.all:
        full_uninstall(dry_run=args.dry_run, yes=args.yes)
        return 0

    if args.sub_agent:
        env = _read_env()
        names = [n.strip() for n in args.sub_agent.split(",") if n.strip()]
        for name in names:
            remove_sub_agent(name, env, dry_run=args.dry_run)
            env = _read_env()  # re-read after each, so Google-family guard sees the updates
        # If this call covers the entire Google bundle, also delete the
        # shared pickle + credentials.json. The per-sub-agent guard
        # can't tell from env-state alone that the bundle is empty
        # (Google sub-agents have no env_vars to clear), so we do it here.
        if GOOGLE_FAMILY.issubset(set(names)):
            _hr("Removing shared Google OAuth state")
            for relpath in ("data/google_token.pickle", "config/credentials.json"):
                p = V1_DIR / relpath
                if not p.exists():
                    _info(f"no {relpath}")
                    continue
                if args.dry_run:
                    _info(f"would delete: {relpath}")
                else:
                    p.unlink()
                    _ok(f"deleted: {relpath}")
        return 0

    if args.launchagents:
        remove_launchagents(dry_run=args.dry_run)
        return 0

    if args.data:
        remove_data(dry_run=args.dry_run)
        return 0

    # No flag → interactive menu.
    interactive(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted.")
        sys.exit(130)
