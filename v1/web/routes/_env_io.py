"""Shared `.env` read + comment-preserving writer.

Used by the install wizard, the transport picker, and the `/config/env`
editor. Centralizes the "rewrite values in-place, append unknown keys,
preserve comments + blank lines" logic that used to be duplicated.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from core.paths import env_path

# Back-compat: V1_DIR + ENV_PATH are imported by other modules. Keep
# them defined here as resolved at import time. New code should call
# env_path() / source_dir() / etc. via core.paths directly.
V1_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = env_path()


def read_env_dict() -> dict[str, str]:
    """Read `.env` into a flat str→str dict. Empty if the file is
    missing. Empty values become ""."""
    if not ENV_PATH.exists():
        return {}
    parsed = dotenv_values(ENV_PATH) or {}
    return {k: (v or "") for k, v in parsed.items()}


def write_env_values(
    updates: dict[str, str],
    append_header: str = "# ── Added from install wizard ──",
) -> None:
    """Write `updates` into `.env` non-destructively.

    For each key in `updates`:
      * if the key already exists in `.env`, the value is replaced
        in-place (preserves the comment block above it);
      * otherwise the key gets appended at the bottom under
        `append_header`, with a blank-line separator if the previous
        line wasn't blank.

    Comments + blank lines elsewhere in `.env` are preserved
    verbatim. The file's mode is forced to `0o600` after every
    write (security batch 1 / ROADMAP H1).
    """
    if not ENV_PATH.exists():
        # First-run case: create a minimal .env. The wizard's welcome
        # step normally seeds from `.env.example`, so this branch is
        # mostly a defensive fallback.
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        ENV_PATH.write_text("\n".join(f"{k}={v}" for k, v in updates.items()) + "\n")
        os.chmod(ENV_PATH, 0o600)
        return

    existing_lines = ENV_PATH.read_text().splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = line.partition("=")[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)

    appended: list[str] = [f"{k}={v}" for k, v in updates.items() if k not in seen]
    if appended:
        if out and out[-1].strip():
            out.append("")
        out.append(append_header)
        out.extend(appended)

    ENV_PATH.write_text("\n".join(out) + "\n")
    os.chmod(ENV_PATH, 0o600)
