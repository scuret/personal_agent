"""One-shot fix for file permissions on personal_agent state.

The auth scripts and SQLite layer write tokens / DB / logs with the
process umask. On a typical Mac that ends up `0o644` (world-readable
on the local machine — any other OS user, or a malicious app running
under another account, can `cat` your live refresh tokens). New writes
go out at `0o600` (ROADMAP H1), but files that already exist on disk
from earlier runs keep their old mode until something rewrites them.

This tool one-shots through every known-sensitive path under v1/ and
forces it to `0o600`. Safe to re-run.

Manual:
    python -m tools.repair_permissions          # apply the fix
    python -m tools.repair_permissions --dry-run  # just print what'd change
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

from core.paths import (
    config_dir,
    credentials_path,
    data_dir,
    env_path,
    source_dir,
    triggers_yaml_path,
)

V1_DIR = source_dir()
DATA_DIR = data_dir()
CONFIG_DIR = config_dir()

# Single sensitive files we always want owner-only.
SENSITIVE_FILES: list[Path] = [
    env_path(),
    credentials_path(),
    triggers_yaml_path(),
]

# Glob patterns under data/ that we want owner-only.
SENSITIVE_GLOBS: list[str] = [
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite-journal",
    "*.pickle",
    "*_token.json",
    "*.log",
    "*.log.*",  # rotated copies
    "*.json",   # catch-all for unrecognized token caches
    "eight_token.json",
]

TARGET_MODE = 0o600


def _current_mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _format_mode(m: int) -> str:
    return f"0o{m:03o}"


def repair(dry_run: bool = False) -> None:
    print("=== personal_agent permission repair ===")
    print(f"target mode: {_format_mode(TARGET_MODE)} (owner read/write only)")
    print(f"v1 dir:      {V1_DIR}")
    if dry_run:
        print("(dry run — no changes)")
    print()

    paths: list[Path] = []
    for p in SENSITIVE_FILES:
        if p.exists():
            paths.append(p)
    if DATA_DIR.exists():
        for pattern in SENSITIVE_GLOBS:
            for p in DATA_DIR.glob(pattern):
                if p.is_file():
                    paths.append(p)

    # Dedupe (the globs can overlap).
    seen: set[Path] = set()
    fixed = 0
    already = 0
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        try:
            cur = _current_mode(p)
        except OSError as e:
            print(f"  ✗ {p}: {e}")
            continue
        if cur == TARGET_MODE:
            already += 1
            continue
        if dry_run:
            print(f"  would chmod {_format_mode(cur)} → {_format_mode(TARGET_MODE)}: {p}")
        else:
            try:
                os.chmod(p, TARGET_MODE)
                print(f"  fixed {_format_mode(cur)} → {_format_mode(TARGET_MODE)}: {p}")
                fixed += 1
            except OSError as e:
                print(f"  ✗ {p}: {e}")

    print()
    print(f"scanned: {len(seen)}")
    print(f"already at target: {already}")
    if dry_run:
        print(f"would fix: {len(seen) - already}")
    else:
        print(f"fixed: {fixed}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Force owner-only (0o600) permissions on personal_agent state files."
    )
    ap.add_argument("--dry-run", action="store_true", help="Print what would change, don't change anything.")
    args = ap.parse_args()
    repair(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
