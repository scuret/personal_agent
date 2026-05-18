"""Store the Eight Sleep account password in the macOS Keychain.

`mcp_servers/eightsleep_auth.py` looks here first before falling back
to the plaintext `EIGHT_PASSWORD` in `.env`. Run this once after
install (or any time you need to rotate the password):

    python -m tools.eightsleep_set_password

It will:
  1. Read `EIGHT_EMAIL` from your `.env` (so the keyring entry is
     keyed correctly).
  2. Prompt for the password (hidden input via `getpass`).
  3. Store it under service `personal_agent_eight_sleep`, account
     `<your email>` in the OS keyring (macOS Keychain on Mac).

If you've previously kept the password in `EIGHT_PASSWORD`, this tool
will also offer to clear that env var from `.env` so it doesn't sit
on disk in plaintext.

Backends:
  * macOS — Keychain (login keyring)
  * Linux — Secret Service (libsecret); needs a D-Bus session
  * Windows — Credential Locker

If no backend is available (headless Linux, CI), the tool reports
that and you stick with `.env`.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from dotenv import dotenv_values

from core.paths import env_path, source_dir

V1_DIR = source_dir()
ENV_PATH = env_path()

# Match the constants in mcp_servers/eightsleep_auth.py exactly.
KEYRING_SERVICE = "personal_agent_eight_sleep"


def _load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    parsed = dotenv_values(ENV_PATH) or {}
    return {k: (v or "") for k, v in parsed.items()}


def _strip_env_password() -> bool:
    """Set EIGHT_PASSWORD= (empty) in .env if it currently has a value.
    Returns True if a change was made.
    """
    if not ENV_PATH.exists():
        return False
    lines = ENV_PATH.read_text().splitlines()
    changed = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("EIGHT_PASSWORD=") and stripped != "EIGHT_PASSWORD=":
            out.append("EIGHT_PASSWORD=")
            changed = True
        else:
            out.append(line)
    if changed:
        ENV_PATH.write_text("\n".join(out) + "\n")
    return changed


def main() -> int:
    try:
        import keyring
    except ImportError:
        print(
            "✗ `keyring` package isn't installed. Run "
            "`uv pip install -e .` (or pip equivalent) from v1/ to pull "
            "the deps from pyproject.toml, then re-run this tool.",
            file=sys.stderr,
        )
        return 1

    env = _load_env()
    email = (env.get("EIGHT_EMAIL") or "").strip()
    if not email:
        print(
            "✗ EIGHT_EMAIL not set in .env. Add it first (re-run "
            "`./install.sh` to walk through the Eight Sleep step, or "
            "open the /config/env page in the web UI).",
            file=sys.stderr,
        )
        return 1

    try:
        backend = keyring.get_keyring().__class__.__name__
    except Exception as e:
        print(f"✗ no keyring backend available: {e}", file=sys.stderr)
        print("  Stick with EIGHT_PASSWORD in .env on this machine.")
        return 1

    print(f"keyring backend: {backend}")
    print(f"storing password for EIGHT_EMAIL = {email}")
    print(f"under keyring service: {KEYRING_SERVICE}")
    print()

    pw = getpass.getpass("Eight Sleep password (hidden): ").strip()
    if not pw:
        print("✗ empty password — aborted.", file=sys.stderr)
        return 2

    confirm = getpass.getpass("Confirm: ").strip()
    if confirm != pw:
        print("✗ passwords don't match — aborted.", file=sys.stderr)
        return 2

    try:
        keyring.set_password(KEYRING_SERVICE, email, pw)
    except Exception as e:
        print(f"✗ keyring write failed: {e}", file=sys.stderr)
        return 1

    print("✓ stored.")

    if (env.get("EIGHT_PASSWORD") or "").strip():
        print()
        ans = input(
            "EIGHT_PASSWORD is also set in .env. Clear it so the "
            "password only lives in the Keychain? [Y/n] "
        ).strip().lower()
        if ans in ("", "y", "yes"):
            if _strip_env_password():
                print("✓ EIGHT_PASSWORD cleared from .env.")
                print(
                    "  Restart the scheduler so the new auth picks "
                    "up:  launchctl kickstart -k "
                    "gui/$(id -u)/com.personal-agent.scheduler"
                )
            else:
                print("(.env was already empty for that key.)")
        else:
            print(
                "Left EIGHT_PASSWORD in .env. The auth resolver will "
                "prefer the Keychain entry anyway; clear it whenever "
                "you're ready."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
