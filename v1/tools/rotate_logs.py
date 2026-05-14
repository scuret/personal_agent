"""Daily log rotation for the relay and scheduler.

Each daemon writes stdout/stderr to a file managed by launchd
(StandardOutPath / StandardErrorPath). launchd opens those files in
append mode, so we can `copytruncate` (snapshot to a dated backup,
then truncate the live file in-place) without restarting the daemons —
they keep writing to the same inode, just at offset 0 again.

Rotation keeps the last `KEEP_DAYS` dated copies and deletes the rest.
Run daily at ~03:00 via a separate launchd CalendarInterval plist
(launch_agents/com.personal-agent.log-rotation.plist).

Manual run:
    python -m tools.rotate_logs           # rotate now
    python -m tools.rotate_logs --dry-run # show what would happen
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_NAMES: list[str] = [
    "relay.log",
    "relay.err.log",
    "scheduler.log",
    "scheduler.err.log",
    "web.log",
    "web.err.log",
    "log-rotation.log",
    "log-rotation.err.log",
]
KEEP_DAYS = 7

_DATE_SUFFIX = re.compile(r"\.(\d{8})$")  # matches .YYYYMMDD


def _rotate_one(name: str, today: str, dry_run: bool) -> str:
    """Rotate a single log file. Returns a status string for the caller to print."""
    src = DATA_DIR / name
    if not src.exists():
        return f"  skip (missing): {name}"
    if src.stat().st_size == 0:
        return f"  skip (empty):   {name}"

    dst = DATA_DIR / f"{name}.{today}"
    if dry_run:
        return f"  would rotate:   {name} → {dst.name} ({src.stat().st_size} bytes)"

    shutil.copy2(src, dst)
    # Rotated copies inherit `shutil.copy2`'s permission preservation;
    # if the source is world-readable we want the dated copy to be
    # owner-only. Live log perms are tightened separately when the
    # daemons open them (ROADMAP H1 — the in-place src.open('w') below
    # also gets the umask treatment).
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    # In-place truncate. launchd's append-mode FD continues writing
    # to the same inode; the file's apparent size resets to 0.
    with src.open("w") as f:
        f.truncate()
    try:
        os.chmod(src, 0o600)
    except OSError:
        pass
    return f"  rotated:        {name} → {dst.name}"


def _prune_old(today: str, dry_run: bool) -> list[str]:
    """Drop dated rotations older than KEEP_DAYS. Returns log lines."""
    cutoff_dt = datetime.now() - timedelta(days=KEEP_DAYS)
    cutoff = cutoff_dt.strftime("%Y%m%d")
    results: list[str] = []
    for f in DATA_DIR.iterdir():
        if not f.is_file():
            continue
        m = _DATE_SUFFIX.search(f.name)
        if not m:
            continue
        if m.group(1) >= cutoff:
            continue
        # Make sure we only ever delete a rotation of one of OUR logs.
        base = f.name[: m.start()]
        if base not in LOG_NAMES:
            continue
        if dry_run:
            results.append(f"  would prune:    {f.name}")
        else:
            f.unlink()
            results.append(f"  pruned:         {f.name}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate daemon logs.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, don't change anything.")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y%m%d")
    print(f"=== log rotation @ {datetime.now().isoformat()} ===")
    print(f"data dir: {DATA_DIR}")
    print(f"keep days: {KEEP_DAYS}")
    print()
    for name in LOG_NAMES:
        print(_rotate_one(name, today, args.dry_run))
    pruned = _prune_old(today, args.dry_run)
    if pruned:
        print()
        for line in pruned:
            print(line)


if __name__ == "__main__":
    main()
