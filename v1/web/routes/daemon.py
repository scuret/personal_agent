"""Daemon control + log tailing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from web import daemon_control
from web.app import templates

router = APIRouter(prefix="/daemon")


@router.get("/status-fragment", response_class=HTMLResponse)
async def status_fragment(request: Request) -> HTMLResponse:
    """HTMX-polled fragment — returns just the inner grid of daemon
    tiles, not the whole page."""
    return templates.TemplateResponse(
        request, "_daemon_grid.html",
        {"daemons": daemon_control.status()},
    )


@router.post("/{name}/restart")
async def restart_daemon(name: str) -> dict:
    if name not in daemon_control.DAEMONS:
        raise HTTPException(404, f"unknown daemon: {name}")
    label = daemon_control.DAEMONS[name]["label"]
    ok, err = daemon_control.restart(label)
    if not ok:
        raise HTTPException(500, f"restart failed: {err}")
    return {"ok": True, "name": name}


@router.get("/{name}/log-stream")
async def log_stream(name: str):
    """SSE-tailed log — one event per line, follows the file as it grows."""
    if name not in daemon_control.DAEMONS:
        raise HTTPException(404, f"unknown daemon: {name}")

    async def event_source():
        async for line in daemon_control.tail_log(name, follow=True, lines=300):
            # Wrap each line in a <div> so htmx's beforeend swap renders
            # them as distinct block elements (preserves line breaks).
            # HTML-escape to neutralize any < / > / & in log content.
            from html import escape as _esc
            safe = _esc(line or " ")
            yield {"data": f"<div>{safe}</div>"}

    return EventSourceResponse(event_source())
