"""Conversation history browser."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from memory.store import MemoryStore
from web.app import templates

router = APIRouter()


def _store() -> MemoryStore:
    return MemoryStore()


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, q: str | None = Query(None), limit: int = Query(50, ge=1, le=200)) -> HTMLResponse:
    store = _store()
    if q:
        convs = store.search_conversations(q, limit=limit)
    else:
        rows = store._conn().execute(  # noqa: SLF001
            """SELECT c.id, c.source, c.started_at, c.ended_at,
                      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count
                 FROM conversations c
                ORDER BY c.started_at DESC
                LIMIT ?""", (limit,)
        )
        convs = [dict(r) for r in rows]
    return templates.TemplateResponse(
        request, "conversations/list.html",
        {"convs": convs, "q": q, "limit": limit},
    )


@router.get("/history/{conv_id}", response_class=HTMLResponse)
async def detail(request: Request, conv_id: str) -> HTMLResponse:
    store = _store()
    conv = store._conn().execute(  # noqa: SLF001
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if not conv:
        raise HTTPException(404, f"conversation not found: {conv_id}")
    messages = store._conn().execute(  # noqa: SLF001
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()
    msgs: list[dict[str, Any]] = []
    for m in messages:
        d = dict(m)
        if d.get("tool_calls"):
            try:
                d["tool_calls_parsed"] = json.loads(d["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                d["tool_calls_parsed"] = None
        msgs.append(d)
    return templates.TemplateResponse(
        request, "conversations/detail.html",
        {"conv": dict(conv), "messages": msgs},
    )
