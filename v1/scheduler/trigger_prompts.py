"""Per-trigger few-shot example assembly.

When the user gives feedback on a trigger ("this email should have
pinged me" / "the morning brief should NOT have included the weather
section") the agent captures it as a `trigger_examples` row via
`mcp_servers.learning_server`. At call time, each trigger's prompt
assembly invokes `render_examples_block` to fetch the most recent
positive + negative corrections and inject them into the system /
user prompt as in-context examples.

Design:
  * Bounded prompt budget — at most `max_per_polarity` examples per
    polarity (default 3) so the Haiku triage call doesn't pay an
    unbounded context-window tax.
  * Soft-delete aware — only `is_active = 1` rows ship.
  * Newest first within each polarity so the most recent user
    correction has the strongest in-context recency bias.
  * Returns `""` when there are no examples for the trigger — the
    caller appends without an `if` guard and the prompt stays
    identical to the pre-learning behavior.

Truncation:
  Long `input_payload` values (email bodies especially) are truncated
  to `payload_chars` (default 800) per example. We want enough for
  the model to recognize the pattern, not the full body. The user's
  `expected_output` and `note` ride through unchanged — they're
  always short.
"""

from __future__ import annotations

from typing import Any

from memory.store import MemoryStore


_DEFAULT_PAYLOAD_CHARS = 800
_DEFAULT_MAX_PER_POLARITY = 3


def render_examples_block(
    trigger_name: str,
    store: MemoryStore,
    max_per_polarity: int = _DEFAULT_MAX_PER_POLARITY,
    payload_chars: int = _DEFAULT_PAYLOAD_CHARS,
) -> str:
    """Build the prompt block for `trigger_name` or return ''.

    The caller should prepend this to whichever prompt the trigger
    sends to the LLM — system prompt for the email-triage one-shot,
    user-facing prompt body for the brief / weekly review.
    """
    positives = store.list_trigger_examples(
        trigger_name=trigger_name,
        polarity="positive",
        limit=max_per_polarity,
        active_only=True,
    )
    negatives = store.list_trigger_examples(
        trigger_name=trigger_name,
        polarity="negative",
        limit=max_per_polarity,
        active_only=True,
    )

    if not positives and not negatives:
        return ""

    lines: list[str] = [
        "## Past corrections the user has given you (most recent first)",
        "",
        "Each example below is a real case where the user told you the trigger",
        "got it wrong (negative) or should have fired but didn't (positive).",
        "Treat these as soft preferences — match the spirit, don't pattern-",
        "match verbatim. If the current input is meaningfully different,",
        "decide on its own merits.",
        "",
    ]

    n = 1
    for ex in positives:
        lines.append(f"EXAMPLE {n} — positive (should fire / output was correct):")
        lines.extend(_render_example(ex, payload_chars))
        lines.append("")
        n += 1
    for ex in negatives:
        lines.append(f"EXAMPLE {n} — negative (should NOT fire / output was wrong):")
        lines.extend(_render_example(ex, payload_chars))
        lines.append("")
        n += 1

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _render_example(ex: dict[str, Any], payload_chars: int) -> list[str]:
    payload = (ex.get("input_payload") or "").strip()
    if len(payload) > payload_chars:
        # Truncate from the middle isn't worth the complexity here; keep
        # the head, which usually carries the signal (sender / subject /
        # opening line for emails; brief context preamble for briefs).
        payload = payload[:payload_chars].rstrip() + " […truncated]"
    out = [f"  Input: {payload}"]
    expected = (ex.get("expected_output") or "").strip()
    if expected:
        out.append(f"  Expected: {expected}")
    note = (ex.get("note") or "").strip()
    if note:
        out.append(f"  Why: {note}")
    return out
