"""Reminders viewer — read-only in Phase 1."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from memory.store import MemoryStore
from web.templating import templates

router = APIRouter()


@router.get("/reminders", response_class=HTMLResponse)
async def list_reminders(request: Request) -> HTMLResponse:
    store = MemoryStore()
    pending = store.list_pending_reminders(limit=200)
    # Also pull recent fired ones for context.
    recent_fired = store._conn().execute(  # noqa: SLF001
        """SELECT id, message, fire_at, fired_at, recurrence_rule
             FROM reminders
            WHERE fired_at IS NOT NULL
            ORDER BY fired_at DESC LIMIT 30"""
    ).fetchall()
    return templates.TemplateResponse(
        request, "reminders/list.html",
        {
            "pending": pending,
            "recent_fired": [dict(r) for r in recent_fired],
        },
    )
