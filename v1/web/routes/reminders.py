"""Reminders viewer — list + create + cancel."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from memory.store import MemoryStore
from web.templating import templates

router = APIRouter()


def _user_tz() -> ZoneInfo:
    name = os.environ.get("USER_TIMEZONE", "America/Chicago")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/Chicago")


def _to_utc_iso(local_dt: str) -> str:
    """Parse 'YYYY-MM-DDTHH:MM' (datetime-local input) in USER_TIMEZONE,
    return UTC ISO-8601 with seconds (the format the scheduler compares).
    """
    naive = datetime.fromisoformat(local_dt)
    aware = naive.replace(tzinfo=_user_tz())
    return aware.astimezone(UTC).isoformat(timespec="seconds")


@router.get("/reminders", response_class=HTMLResponse)
async def list_reminders(request: Request) -> HTMLResponse:
    store = MemoryStore()
    pending = store.list_pending_reminders(limit=200)
    recent_fired = store._conn().execute(
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
            "user_timezone": str(_user_tz()),
        },
    )


@router.post("/reminders")
async def create_reminder(
    message: str = Form(...),
    fire_at_local: str = Form(...),
    recurrence: str = Form(""),
) -> RedirectResponse:
    """Schedule a one-off or recurring reminder.

    fire_at_local is the user's local time in 'YYYY-MM-DDTHH:MM' form
    (the HTML5 datetime-local input shape). The scheduler stores fire_at
    in UTC ISO-8601, so we convert here using USER_TIMEZONE.

    recurrence is one of "" / "daily" / "weekdays" / "weekly" / "monthly".
    Stored as the `recurrence_rule` JSON the scheduler understands.
    """
    msg = (message or "").strip()
    if not msg:
        raise HTTPException(400, "reminder message is required")
    if not fire_at_local:
        raise HTTPException(400, "fire time is required")
    try:
        fire_at_utc = _to_utc_iso(fire_at_local)
    except ValueError as e:
        raise HTTPException(400, f"invalid datetime: {e}") from e

    rule: dict | None = None
    rec = (recurrence or "").strip().lower()
    if rec in {"daily", "weekdays", "weekly", "monthly"}:
        rule = {"type": rec}

    store = MemoryStore()
    store.schedule_reminder(fire_at=fire_at_utc, message=msg, recurrence_rule=rule)
    return RedirectResponse("/reminders", status_code=303)


@router.post("/reminders/{reminder_id}/cancel")
async def cancel_reminder(reminder_id: int) -> RedirectResponse:
    store = MemoryStore()
    if not store.cancel_reminder(reminder_id):
        raise HTTPException(404, "reminder not pending")
    return RedirectResponse("/reminders", status_code=303)
