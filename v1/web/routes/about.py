"""Static disclosure pages. /about/privacy mirrors the README's
'Privacy & security profile' section so users can read it without
leaving the web UI."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.templating import templates

router = APIRouter(prefix="/about")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "about/privacy.html", {})
