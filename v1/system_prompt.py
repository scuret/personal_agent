"""System prompt assembly.

Loads `config/personality.md` (the editable source-of-truth for the agent's
voice + operating rules) and stitches in dynamic context: current date/time,
user timezone, and — once memory lands in step 3 — extracted user facts.

In step 2 there are no integrations yet, so the dynamic section just
contains date/time and timezone.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytz

# config/personality.md sits one level inside this package; resolve relative to
# this file so the script can run from any working directory.
_PERSONALITY_PATH = Path(__file__).parent / "config" / "personality.md"


def _load_personality() -> str:
    return _PERSONALITY_PATH.read_text(encoding="utf-8").strip()


def _build_runtime_context() -> str:
    tz_name = os.environ.get("USER_TIMEZONE", "America/Chicago")
    try:
        tz = pytz.timezone(tz_name)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.timezone("America/Chicago")
        tz_name = "America/Chicago (fallback — USER_TIMEZONE was invalid)"

    now = datetime.now(tz)
    return (
        "## Runtime context\n"
        f"- current date: {now.strftime('%A, %B %d, %Y')}\n"
        f"- current time: {now.strftime('%I:%M %p').lstrip('0').lower()}\n"
        f"- timezone: {tz_name}\n"
    )


def build_system_prompt() -> str:
    """Return the full system prompt: personality + runtime context."""
    return f"{_load_personality()}\n\n---\n\n{_build_runtime_context()}"


if __name__ == "__main__":
    # Useful for debugging — `python system_prompt.py` prints the assembled prompt.
    print(build_system_prompt())
