"""System prompt assembly.

Loads `config/personality.md` (the human-editable source-of-truth tone +
operating rules) and stitches in dynamic context: extracted user memories,
the current date/time/timezone, the active integrations.

The output is the full system prompt passed to Claude on every turn.

Built in step 2 of the v1 plan.
"""


def build_system_prompt() -> str:
    raise NotImplementedError("System prompt builder not yet implemented — see step 2.")
