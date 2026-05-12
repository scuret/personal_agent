"""Shared AppleScript helpers for the Apple-native sub-agents.

Wraps `py-applescript` (already in pyproject for the iMessage relay)
with a tiny convenience API every Apple-native MCP server uses. Keeps
quote-escaping and error handling in one place rather than duplicated
across reminders / notes / photos / music / mail servers.

Available only on macOS — every Apple-native server gates its
registration in agent_host via `sys.platform == "darwin"`.
"""

from __future__ import annotations

import sys
from typing import Any


def is_macos() -> bool:
    return sys.platform == "darwin"


def escape_str(s: str) -> str:
    """Escape a Python string for embedding inside an AppleScript double-
    quoted string literal. Order matters — backslashes first, then quotes."""
    if s is None:
        return ""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def run_script(source: str) -> str:
    """Execute an AppleScript source and return its (string) result.

    Raises `RuntimeError` on script error (with the AppleScript error
    message included), so callers can `try/except RuntimeError` and
    return an `_err(...)` MCP response.
    """
    if not is_macos():
        raise RuntimeError("AppleScript is only available on macOS")
    import applescript  # late import — only paid on macOS sub-agents

    try:
        result = applescript.AppleScript(source=source).run()
    except applescript.ScriptError as e:
        raise RuntimeError(f"AppleScript error: {e}") from e
    if result is None:
        return ""
    return str(result)


def run_script_for_list(source: str, sep: str = "\n") -> list[str]:
    """Convenience: run an AppleScript that returns text rows separated
    by `sep`, return a Python list of strings (whitespace-stripped,
    empties dropped)."""
    raw = run_script(source)
    if not raw:
        return []
    return [line.strip() for line in raw.split(sep) if line.strip()]


def err(msg: str) -> dict[str, Any]:
    """Standard MCP error response. Apple-native servers use this so all
    AppleScript failures look the same to the agent."""
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}
