"""System prompt assembly.

Loads `config/personality.md` (the editable source-of-truth for the agent's
voice + operating rules) and stitches in dynamic context:
  - Current date/time/timezone
  - Top-N stored facts about the principal (from MemoryStore), so the
    agent doesn't have to call memory_recall_facts on every turn.

In step 3 the facts section becomes available; before any facts are logged
it'll be empty. As the principal interacts with the agent over time, this
section grows.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytz

from memory.store import MemoryStore

_PERSONALITY_PATH = Path(__file__).parent / "config" / "personality.md"

# How many facts to inject up front. Plenty for personal-scale data; the
# agent can call memory_recall_facts for more if needed.
_FACTS_INJECTION_LIMIT = 50


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


def _build_facts_block(store: MemoryStore | None) -> str:
    if store is None:
        return ""
    facts = store.recall_facts(limit=_FACTS_INJECTION_LIMIT)
    if not facts:
        return (
            "## What I know about the principal\n"
            "(no stored facts yet — I'll capture what I learn via memory_log_fact)\n"
        )

    by_category: dict[str, list[str]] = {}
    for f in facts:
        by_category.setdefault(f["category"], []).append(f["content"])

    lines = ["## What I know about the principal"]
    for category in sorted(by_category):
        lines.append(f"\n### {category}")
        for content in by_category[category]:
            lines.append(f"- {content}")
    return "\n".join(lines) + "\n"


def build_system_prompt(store: MemoryStore | None = None) -> str:
    """Return the full system prompt: personality + runtime + stored facts.

    `store` is optional so this module stays usable in scripts/tests that
    don't want to touch SQLite. agent_host always passes it in.
    """
    sections = [_load_personality(), "---", _build_runtime_context()]
    facts = _build_facts_block(store)
    if facts:
        sections.append(facts)
    return "\n\n".join(sections)


if __name__ == "__main__":
    # `python system_prompt.py` prints the assembled prompt for debugging.
    # Includes facts if the DB exists.
    db_path = Path(__file__).parent / "data" / "memory.sqlite"
    s = MemoryStore() if db_path.exists() else None
    print(build_system_prompt(s))
