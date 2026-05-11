"""One-off trigger endpoints — fire briefs, deliveries, email watch on demand."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from memory.store import MemoryStore

router = APIRouter(prefix="/trigger")


@router.post("/morning_brief")
async def morning_brief() -> dict:
    """Fire a morning brief. Runs on Opus (TRIGGER_MODEL) — same as the
    7:30am scheduled fire."""
    from scheduler.triggers import _fire_trigger

    try:
        await _fire_trigger("morning_brief")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"trigger failed: {e}") from e
    return {"ok": True, "fired_at": datetime.now(timezone.utc).isoformat()}


@router.post("/weekly_review")
async def weekly_review() -> dict:
    from scheduler.triggers import _fire_trigger

    try:
        await _fire_trigger("weekly_review")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"trigger failed: {e}") from e
    return {"ok": True, "fired_at": datetime.now(timezone.utc).isoformat()}


@router.post("/delivery_watch")
async def delivery_watch(reset_seen: bool = False) -> dict:
    """Run a delivery_watch pass. Mirrors `--run-now delivery_watch`.
    Pass reset_seen=true to clear the dedup set first (useful for testing
    re-detection of an email you've already been alerted on)."""
    from scheduler.triggers import (
        _DELIVERY_WATCH_LAST_CHECK_KEY,
        _DELIVERY_WATCH_SEEN_KEY,
        _fire_delivery_watch,
        _load_config,
    )

    store = MemoryStore()
    if reset_seen:
        store.set_state(_DELIVERY_WATCH_SEEN_KEY, "[]")
        store.set_state(_DELIVERY_WATCH_LAST_CHECK_KEY, "")
    config = _load_config()
    try:
        _fire_delivery_watch(store, config, datetime.now(timezone.utc))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"delivery_watch failed: {e}") from e
    return {"ok": True}


@router.post("/email_watch")
async def email_watch() -> dict:
    from scheduler.triggers import _fire_email_watch, _load_config

    store = MemoryStore()
    config = _load_config()
    try:
        _fire_email_watch(store, config, datetime.now(timezone.utc))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"email_watch failed: {e}") from e
    return {"ok": True}


@router.post("/reminders")
async def fire_due_reminders() -> dict:
    """Flush all reminders whose fire_at has passed (without waiting
    for the next 30s scheduler tick)."""
    from scheduler.triggers import _fire_due_reminders

    store = MemoryStore()
    try:
        _fire_due_reminders(store)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"reminders failed: {e}") from e
    return {"ok": True}
