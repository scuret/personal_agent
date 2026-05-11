"""Per-conversation ClaudeSDKClient pool.

The web chat surface keeps one live SDK session per active conversation
(matching the pattern the iMessage / Telegram relays use). Keeps the
prompt cache warm across turns within a chat, avoids the per-turn
client startup cost (~250-500ms), and naturally evicts idle sessions
after a configurable timeout.

Eviction: a background task scans the pool every 60s and closes any
session that's been idle longer than IDLE_TIMEOUT.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from claude_agent_sdk import ClaudeSDKClient

from agent_host import build_options
from memory.store import MemoryStore

# Idle eviction: close sessions that haven't been touched in this long.
IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes


@dataclass
class _Entry:
    client: ClaudeSDKClient
    last_used: float = field(default_factory=time.monotonic)


class ClientPool:
    """Keyed by conversation_id. Async-safe via a single lock per pool."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._eviction_task: asyncio.Task | None = None

    async def get(self, conversation_id: str, store: MemoryStore) -> ClaudeSDKClient:
        """Return a live client for this conversation, opening one if needed."""
        async with self._lock:
            entry = self._entries.get(conversation_id)
            if entry is not None:
                entry.last_used = time.monotonic()
                return entry.client

            # Open a new session. The client is an async context manager;
            # we manually __aenter__ here and __aexit__ on eviction.
            options = build_options(store)
            client = ClaudeSDKClient(options=options)
            await client.__aenter__()
            self._entries[conversation_id] = _Entry(client=client)
            return client

    async def close(self, conversation_id: str) -> None:
        async with self._lock:
            entry = self._entries.pop(conversation_id, None)
        if entry is not None:
            try:
                await entry.client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    async def close_all(self) -> None:
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            try:
                await entry.client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    async def _evict_idle(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            async with self._lock:
                stale = [
                    cid for cid, e in self._entries.items()
                    if now - e.last_used > IDLE_TIMEOUT_SECONDS
                ]
                stale_entries = [self._entries.pop(cid) for cid in stale]
            for e in stale_entries:
                try:
                    await e.client.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

    def start_eviction(self) -> None:
        if self._eviction_task is None or self._eviction_task.done():
            self._eviction_task = asyncio.create_task(self._evict_idle())


# Module-level singleton — the route handlers all share the same pool.
POOL = ClientPool()
