"""launchctl wrappers for the web UI.

The web UI needs to check daemon health, restart daemons on config
changes, and tail their log files. Shells out to `launchctl` for state
queries (cheaper + safer than re-implementing launchd in Python) and
opens log files directly for tailing.

Labels currently managed:
  com.personal-agent.relay
  com.personal-agent.scheduler
  com.personal-agent.log-rotation
  com.personal-agent.webui              (the web UI itself)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

V1_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = V1_DIR / "data"

# Display name → launchd label + log basename. Display name is what
# templates render; label is what launchctl sees; log_basename is the
# file under data/ (we append .log / .err.log when tailing).
DAEMONS: dict[str, dict[str, str]] = {
    "relay":        {"label": "com.personal-agent.relay",        "log_basename": "relay"},
    "scheduler":    {"label": "com.personal-agent.scheduler",    "log_basename": "scheduler"},
    "log-rotation": {"label": "com.personal-agent.log-rotation", "log_basename": "log-rotation"},
    "web":          {"label": "com.personal-agent.webui",          "log_basename": "web"},
}


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def is_running(label: str) -> bool:
    """True if the LaunchAgent is currently loaded (whether running or
    idle waiting on KeepAlive — both report exit 0 from `launchctl print`)."""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"{_gui_domain()}/{label}"],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


def get_pid(label: str) -> int | None:
    """Return PID of a running daemon, or None if not currently running.

    `launchctl print` includes a `pid = NNN` line when the job's actually
    spawned. Parse it out. Returns None for loaded-but-not-running jobs
    (e.g. log-rotation which only fires once a day)."""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"{_gui_domain()}/{label}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        s = line.strip()
        if s.startswith("pid ="):
            try:
                return int(s.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def restart(label: str) -> tuple[bool, str]:
    """kickstart -k: stop the daemon and start a fresh instance.
    Returns (ok, error_text)."""
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", f"{_gui_domain()}/{label}"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    return True, ""


def status() -> list[dict[str, object]]:
    """Snapshot of all known daemons. Returns one row per daemon for
    the dashboard's status panel."""
    out: list[dict[str, object]] = []
    for name, cfg in DAEMONS.items():
        label = cfg["label"]
        loaded = is_running(label)
        pid = get_pid(label) if loaded else None
        out.append({
            "name": name,
            "label": label,
            "loaded": loaded,
            "pid": pid,
            "log_path": str(DATA_DIR / f"{cfg['log_basename']}.log"),
        })
    return out


def log_path(name: str, kind: str = "log") -> Path:
    """Path to a daemon log file. kind ∈ {'log', 'err.log'}."""
    cfg = DAEMONS.get(name)
    if not cfg:
        raise ValueError(f"unknown daemon: {name}")
    suffix = "log" if kind == "log" else "err.log"
    return DATA_DIR / f"{cfg['log_basename']}.{suffix}"


async def tail_log(name: str, follow: bool = True, lines: int = 200) -> AsyncIterator[str]:
    """Async generator yielding lines from a daemon log file.

    On first call, yields the last `lines` lines synchronously, then
    (if follow=True) sleeps and polls for new content. Suitable for
    feeding an SSE response — each yielded line becomes one event.
    """
    path = log_path(name)
    if not path.exists():
        yield f"[log file not yet created: {path}]"
        if not follow:
            return

    # Seed with the tail of existing content.
    if path.exists():
        with path.open("r", errors="replace") as f:
            # Cheap tail: read everything, take the last N lines. For
            # daemon logs this is fine (~MB-class files at worst, and
            # log-rotation runs daily to keep them small).
            existing = f.readlines()[-lines:]
        for line in existing:
            yield line.rstrip("\n")
        last_size = path.stat().st_size
    else:
        last_size = 0

    if not follow:
        return

    # Poll for growth. 500ms cadence is responsive enough for human
    # eyes and cheap on a daemon log that may go minutes between writes.
    while True:
        await asyncio.sleep(0.5)
        if not path.exists():
            continue
        size = path.stat().st_size
        if size <= last_size:
            # File got rotated or truncated — start from the new beginning.
            if size < last_size:
                last_size = 0
            continue
        with path.open("r", errors="replace") as f:
            f.seek(last_size)
            new = f.read()
        last_size = size
        for line in new.splitlines():
            yield line
