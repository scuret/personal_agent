"""Memory MCP server — conversation archive, fact extraction, audit log.

Three responsibilities, three SQLite stores in `data/`:

1. CONVERSATION ARCHIVE (`data/conversations.sqlite`)
   Every turn (user + assistant + tool calls) is logged with conversation_id
   and timestamp. Searchable by content. Source of truth for "what did we
   talk about last Tuesday".

2. EXTRACTED FACTS (`data/memory.sqlite`)
   A periodic background pass distills durable facts/preferences from recent
   conversations and stores them with a confidence score. The agent host
   pulls the top-N relevant facts at turn-start and injects them into the
   system prompt.

3. AUDIT LOG (`data/audit.sqlite`)
   Every Claude API call (full prompt, full response, tool uses, token
   counts) is logged for user review. Privacy invariant: nothing the agent
   sends to Anthropic is invisible to the user. Written by an SDK hook,
   not by the agent's reasoning loop.

Tools exposed to the agent:
  - memory_search(query, limit?)              — search past conversations
  - memory_recall_facts(category?, limit?)    — pull stored facts
  - memory_log_fact(content, category, ...)   — agent-driven fact capture

Built in step 3 of the v1 plan (FIRST integration to land — the audit log
is a v1 invariant and must exist before any sensitive data flows).
"""


def main() -> None:
    raise NotImplementedError("Memory MCP server not yet implemented — see step 3 of the v1 plan.")


if __name__ == "__main__":
    main()
