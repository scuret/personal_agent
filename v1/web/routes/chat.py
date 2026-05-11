"""Web chat surface — SSE-streamed responses, conversation continuity."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from agent_host import process_turn_stream
from memory.store import MemoryStore
from web.app import templates
from web.sessions import POOL

router = APIRouter()

# Source tag for web-originated conversations. Continuity uses
# resume_or_open_conversation('web') with a 4h gap (same as relays).
WEB_SOURCE = "web"
CONVERSATION_GAP_HOURS = 4.0


def _store() -> MemoryStore:
    return MemoryStore()


@router.get("/chat", response_class=HTMLResponse)
async def chat_shell(request: Request) -> HTMLResponse:
    """Chat page — resumes the most recent web conversation if one's
    still within the gap window, otherwise opens a fresh one."""
    store = _store()
    conv_id = store.resume_or_open_conversation(
        source=WEB_SOURCE, gap_threshold_hours=CONVERSATION_GAP_HOURS
    )
    # Load history for this conversation.
    rows = store._conn().execute(  # noqa: SLF001
        "SELECT role, content, tool_calls, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()
    messages = []
    for r in rows:
        d = dict(r)
        if d.get("tool_calls"):
            try:
                d["tool_calls_parsed"] = json.loads(d["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                d["tool_calls_parsed"] = None
        messages.append(d)
    return templates.TemplateResponse(
        request, "chat.html",
        {"conversation_id": conv_id, "messages": messages},
    )


@router.post("/chat/{conv_id}/send", response_class=HTMLResponse)
async def send_message(request: Request, conv_id: str, text: str = Form(...)) -> HTMLResponse:
    """Append the user's bubble immediately + return a placeholder
    assistant bubble that will be filled via SSE."""
    if not text.strip():
        raise HTTPException(400, "empty message")
    # The actual store.append_message + agent call happens in /stream;
    # this just renders the optimistic UI.
    return templates.TemplateResponse(
        request, "_chat_pending.html",
        {
            "conversation_id": conv_id,
            "user_text": text,
            "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


@router.get("/chat/{conv_id}/stream")
async def stream(conv_id: str, text: str):
    """SSE: run the user's turn against the live SDK client and stream
    each text chunk as an `event: text` SSE frame. Browser-side, the
    chat-pending bubble subscribes and appends each chunk."""
    store = _store()

    async def event_source():
        try:
            client = await POOL.get(conv_id, store)
        except Exception as e:  # noqa: BLE001
            yield {"event": "error", "data": f"client startup failed: {e}"}
            return
        try:
            async for chunk in process_turn_stream(client, store, conv_id, text):
                kind = chunk.get("event")
                if kind == "text":
                    yield {"event": "text", "data": chunk.get("text", "")}
                elif kind == "tool":
                    yield {"event": "tool", "data": chunk.get("name", "")}
                elif kind == "done":
                    cost = chunk.get("cost_usd")
                    dur = chunk.get("duration_ms")
                    yield {
                        "event": "done",
                        "data": json.dumps({"cost_usd": cost, "duration_ms": dur}),
                    }
        except asyncio.CancelledError:
            # Browser closed the connection mid-stream; nothing to clean up.
            return
        except Exception as e:  # noqa: BLE001
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(event_source())


@router.post("/chat/{conv_id}/end")
async def end_conversation(conv_id: str) -> dict:
    """Close the current conversation explicitly — next /chat opens a
    fresh one. Closes the pooled client too."""
    store = _store()
    store.close_conversation(conv_id)
    await POOL.close(conv_id)
    return {"ok": True}
