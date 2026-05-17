"""Auto-restart the daemon when `.env` changes on disk.

The relay + scheduler load `.env` once at startup via `load_dotenv()`,
so any subsequent change (sub-agent toggled from chat via the config
MCP server, a credential pasted in the web UI, a manual edit) goes
unnoticed until the process restarts. This module provides a small
asyncio task that polls `.env`'s mtime every few seconds and exits
the process when it changes. Both LaunchAgents have `KeepAlive=true`
so launchd respawns immediately with the new env.

The watcher only cares about `.env` — `triggers.yaml` already
auto-reloads inside the scheduler tick, and `personality.md` is
loaded fresh per relay turn via `build_system_prompt`.

Wired in from each daemon's startup (see relay/* and scheduler/triggers.py).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# `.env` is always at the v1/ root.
_V1_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_ENV_PATH = _V1_DIR / ".env"


async def watch_env_and_exit_on_change(
    path: Path | str = _DEFAULT_ENV_PATH,
    poll_seconds: float = 5.0,
    log_prefix: str = "[env-watch]",
) -> None:
    """Poll `path`'s mtime; exit the process when it changes.

    LaunchAgent `KeepAlive=true` respawns the daemon picking up the
    new env. `os._exit(0)` is intentional — `sys.exit` would raise
    SystemExit through asyncio task cleanup, which can deadlock when
    the SDK client has an outstanding stream. Hard exit, no cleanup,
    let launchd start fresh.

    If `.env` is missing at startup (fresh checkout, install wizard
    not yet run), the watcher just keeps polling — when the file
    appears, the next mtime check sets the baseline and subsequent
    edits trigger exits as expected.
    """
    p = Path(path)
    start_mtime: float | None = None
    try:
        start_mtime = p.stat().st_mtime
        print(f"{log_prefix} watching {p} (mtime={start_mtime:.0f})", flush=True)
    except OSError:
        print(f"{log_prefix} {p} not present yet; will start watching once it appears", flush=True)

    while True:
        await asyncio.sleep(poll_seconds)
        try:
            now = p.stat().st_mtime
        except OSError:
            # File still missing (or just got deleted) — skip this tick.
            start_mtime = None
            continue
        if start_mtime is None:
            start_mtime = now
            print(f"{log_prefix} baseline set (mtime={now:.0f})", flush=True)
            continue
        if now > start_mtime:
            print(
                f"{log_prefix} {p.name} changed (mtime {start_mtime:.0f} → "
                f"{now:.0f}); exiting for LaunchAgent respawn",
                file=sys.stderr,
                flush=True,
            )
            # Flush logs and hard-exit. KeepAlive=true brings us back up.
            os._exit(0)


def spawn_env_watcher(loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Task:
    """Convenience helper: schedule the watcher on the current event loop.

    Returns the Task handle. The daemon ignores it — the task only ever
    finishes via os._exit, never normally.
    """
    target_loop = loop or asyncio.get_event_loop()
    return target_loop.create_task(watch_env_and_exit_on_change())
