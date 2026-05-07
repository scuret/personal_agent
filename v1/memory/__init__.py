"""Memory layer — SQLite-backed conversation archive, audit log, and facts.

Public surface:
  - `store.MemoryStore` — connection-managed wrapper around the three table
    groups (conversations/messages, api_events, facts). Used directly by
    agent_host (for archive + audit writes) and by the memory MCP server
    (for fact CRUD).
"""

from memory.store import MemoryStore

__all__ = ["MemoryStore"]
