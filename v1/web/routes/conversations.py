"""Conversation history browser."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from memory.store import MemoryStore
from web.templating import templates

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
async def detail(
    request: Request,
    conv_id: str,
    show_third_party: bool = Query(False),
) -> HTMLResponse:
    """Render one conversation's full message thread.

    Third-party messages (from other group-chat members) are hidden
    by default — they have a separate retention cycle and the user
    usually doesn't want them surfaced in the same view as their own
    history. Pass `?show_third_party=1` to include them. ROADMAP M3.
    """
    store = _store()
    conv = store._conn().execute(  # noqa: SLF001
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if not conv:
        raise HTTPException(404, f"conversation not found: {conv_id}")

    if show_third_party:
        rows = store._conn().execute(  # noqa: SLF001
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
            (conv_id,),
        ).fetchall()
    else:
        rows = store._conn().execute(  # noqa: SLF001
            "SELECT * FROM messages WHERE conversation_id = ? "
            "AND (is_third_party = 0 OR is_third_party IS NULL) "
            "ORDER BY id",
            (conv_id,),
        ).fetchall()

    third_party_count = store._conn().execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM messages "
        "WHERE conversation_id = ? AND is_third_party = 1",
        (conv_id,),
    ).fetchone()["n"]

    msgs: list[dict[str, Any]] = []
    for m in rows:
        d = dict(m)
        if d.get("tool_calls"):
            try:
                d["tool_calls_parsed"] = json.loads(d["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                d["tool_calls_parsed"] = None
        msgs.append(d)
    return templates.TemplateResponse(
        request, "conversations/detail.html",
        {
            "conv": dict(conv),
            "messages": msgs,
            "show_third_party": show_third_party,
            "third_party_count": int(third_party_count or 0),
        },
    )
