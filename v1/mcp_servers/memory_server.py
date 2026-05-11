"""Memory MCP server — exposes fact + search tools to the agent.

This is an *in-process* SDK MCP server (via `create_sdk_mcp_server`), not
a stdio subprocess. That means the tools share the agent_host's MemoryStore
instance directly — no IPC, no JSON-over-pipes overhead.

Three tools surfaced to the agent:

  memory_log_fact(content, category, tags?, confidence?)
      Capture a durable fact about the principal. The agent calls this
      whenever it learns something stable (preferences, relationships,
      schedule, work context, etc.). Per project decision, fact extraction
      is agent-driven — there's no background LLM extraction pass.

  memory_recall_facts(category?, query?, limit?)
      Look up stored facts. Used when the agent thinks it needs more
      context than what was injected into the system prompt at start-up.

  memory_search_conversations(query, limit?)
      Substring search across past conversations. Useful for "did we ever
      talk about X" lookups.

The factory `create_memory_mcp_server(store)` returns an `McpSdkServerConfig`
object you pass to `ClaudeAgentOptions(mcp_servers={"memory": cfg})`. Tools
become callable as `mcp__memory__memory_log_fact` etc. (the SDK's tool-
naming convention is `mcp__<server-name>__<tool-name>`).

Wired in by agent_host in step 3.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from memory.store import MemoryStore

# Categories the agent should classify facts into. Free-form strings are
# allowed (the agent might surface a useful new category), but these are
# the canonical ones we expect to see.
FACT_CATEGORIES = [
    "preference",     # "prefers tea over coffee"
    "work",           # "works at Acme on the platform team"
    "relationship",   # "Sarah is the principal's spouse"
    "schedule",       # "blocks 9-11am Tuesdays for deep work"
    "project",        # "leading the Q3 onboarding redesign"
    "communication",  # "hates phone calls, prefers async"
    "context",        # general context that doesn't fit elsewhere
]


def _format_fact_line(f: dict[str, Any]) -> str:
    tags = f.get("tags") or []
    tag_str = f" [tags: {', '.join(tags)}]" if tags else ""
    conf = f.get("confidence", 1.0)
    conf_str = f" (conf {conf:.1f})" if conf < 1.0 else ""
    return f"- [{f['category']}] {f['content']}{conf_str}{tag_str}"


def create_memory_mcp_server(store: MemoryStore) -> McpSdkServerConfig:
    """Build the in-process MCP server with closure access to the shared store."""

    log_fact_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact itself, in one short declarative sentence.",
            },
            "category": {
                "type": "string",
                "description": (
                    "Category. Prefer one of: "
                    + ", ".join(FACT_CATEGORIES)
                    + ". Other strings allowed if none fit."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for retrieval. 1-3 short keywords.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0.0-1.0. Use ~1.0 for things stated directly; lower for inferences.",
            },
        },
        "required": ["content", "category"],
    }

    @tool(
        "memory_log_fact",
        (
            "Capture a durable fact about the principal. Use when you learn "
            "something stable that should persist across conversations: a "
            "preference, relationship, work detail, schedule, or project "
            "context. Don't log ephemeral state (today's mood, what they're "
            "doing right now)."
        ),
        log_fact_schema,
    )
    async def memory_log_fact(args: dict[str, Any]) -> dict[str, Any]:
        try:
            fact_id = store.log_fact(
                content=args["content"],
                category=args["category"],
                tags=args.get("tags"),
                confidence=float(args.get("confidence", 1.0)),
            )
            return {
                "content": [
                    {"type": "text", "text": f"logged fact #{fact_id}: {args['content']}"}
                ]
            }
        except Exception as e:  # noqa: BLE001 — we want to surface any storage error to the agent
            return {
                "content": [{"type": "text", "text": f"error logging fact: {e}"}],
                "is_error": True,
            }

    recall_facts_schema = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Filter to this category. Omit to search all categories.",
            },
            "query": {
                "type": "string",
                "description": (
                    "Free-text query — semantic search over fact content "
                    "(vector + literal substring re-rank). Omit for "
                    "category-only listing."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Max facts to return. Default 20.",
            },
        },
        "required": [],
    }

    @tool(
        "memory_recall_facts",
        (
            "Retrieve stored facts about the principal. The most relevant "
            "facts are already injected into your system prompt at startup, "
            "so you usually don't need this — call it only when you need to "
            "search facts the system prompt didn't include. With `query`, "
            "uses semantic recall (vector + substring re-rank); without "
            "query, returns the category listing."
        ),
        recall_facts_schema,
    )
    async def memory_recall_facts(args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("query")
        limit = int(args.get("limit", 20))
        if query:
            facts = store.semantic_recall_facts(
                query=query,
                category=args.get("category"),
                limit=limit,
            )
        else:
            facts = store.recall_facts(
                category=args.get("category"),
                limit=limit,
            )
        if not facts:
            return {"content": [{"type": "text", "text": "no facts found."}]}
        body = "\n".join(_format_fact_line(f) for f in facts)
        return {"content": [{"type": "text", "text": body}]}

    search_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language description of what you're looking "
                    "for. Matched semantically across the conversation "
                    "archive (vector embeddings) with a literal-substring "
                    "boost — works for both 'pay credit card' (exact) and "
                    "'that thing about wedding planning' (fuzzy)."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Max conversations to return. Default 10.",
            },
        },
        "required": ["query"],
    }

    @tool(
        "memory_search_conversations",
        (
            "Semantic search across past conversations. Returns the "
            "best-matching conversation summaries with the highest-scoring "
            "snippet from each. Use for 'did we ever talk about X', 'what "
            "was that note about Y', or any time you want to surface "
            "prior context that the current conversation doesn't have."
        ),
        search_schema,
    )
    async def memory_search_conversations(args: dict[str, Any]) -> dict[str, Any]:
        results = store.semantic_search_conversations(
            query=args["query"], limit=int(args.get("limit", 10))
        )
        if not results:
            return {"content": [{"type": "text", "text": "no matching conversations."}]}
        lines = []
        for r in results:
            snippet = (r.get("first_match") or "").strip().replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            lines.append(
                f"- {r['started_at'][:10]} ({r['source']}, {r['message_count']} msgs): {snippet}"
            )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    return create_sdk_mcp_server(
        name="memory",
        version="1.0.0",
        tools=[memory_log_fact, memory_recall_facts, memory_search_conversations],
    )


def main() -> None:
    raise NotImplementedError(
        "memory_server is in-process; instantiate via create_memory_mcp_server(store) from agent_host."
    )


if __name__ == "__main__":
    main()
