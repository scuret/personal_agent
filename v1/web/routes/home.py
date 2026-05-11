"""GET / — dashboard with at-a-glance tiles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from memory.store import MemoryStore
from tools.cost_report import summary as cost_summary
from web import daemon_control
from web.app import templates

router = APIRouter()


def _store() -> MemoryStore:
    return MemoryStore()


def _today_cost() -> dict[str, Any]:
    """Last-24h spend snapshot for the dashboard tile."""
    s = cost_summary(days=1)
    return {
        "turns": s["agent"]["turn_count"],
        "cost": s["agent"]["total_cost"],
        "input_tokens": s["agent"]["input_tokens"],
        "output_tokens": s["agent"]["output_tokens"],
    }


def _next_triggers() -> list[dict[str, str]]:
    """Best-effort: peek at the scheduler's --check output for upcoming
    fires. Falls back to "unknown" if scheduler isn't running."""
    try:
        # Use the scheduler's own diagnostic function so we stay in sync
        # with whatever triggers.yaml is currently configured.
        from scheduler.triggers import _compute_next_fires, _load_config, _user_tz

        config = _load_config()
        now = datetime.now(_user_tz())
        fires = _compute_next_fires(now, config)
        return [
            {"name": name, "when": dt.strftime("%a %b %d %I:%M %p %Z")}
            for name, dt in fires
        ]
    except Exception:  # noqa: BLE001
        return []


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    store = _store()
    daemons = daemon_control.status()
    cost = _today_cost()

    pending_reminders = store.list_pending_reminders(limit=5)

    # Recent activity: last 5 conversations with their source + last
    # message preview. Read straight from the audit log.
    recent = list(store._conn().execute(  # noqa: SLF001
        """SELECT c.id, c.source, c.started_at,
                  (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count
             FROM conversations c
            ORDER BY c.started_at DESC
            LIMIT 5"""
    ))
    recent_convs = [dict(r) for r in recent]

    next_fires = _next_triggers()

    return templates.TemplateResponse(
        request, "home.html",
        {
            "daemons": daemons,
            "cost": cost,
            "pending_reminders": pending_reminders,
            "recent_convs": recent_convs,
            "next_fires": next_fires,
            "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
