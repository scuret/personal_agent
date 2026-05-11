"""Observability dashboards: cost, analytics, token health, log streams."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from tools.analytics import analytics_data
from tools.cost_report import summary as cost_summary
from tools.token_health import run_checks
from web import daemon_control
from web.app import templates

router = APIRouter(prefix="/observability")


@router.get("", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "observability/index.html",
        {"daemons": list(daemon_control.DAEMONS.keys())},
    )


@router.get("/cost", response_class=HTMLResponse)
async def cost(request: Request, days: int = Query(7, ge=1, le=90)) -> HTMLResponse:
    data = cost_summary(days=days)
    return templates.TemplateResponse(
        request, "observability/cost.html",
        {"data": data, "days": days},
    )


@router.get("/analytics", response_class=HTMLResponse)
async def analytics(request: Request, days: int = Query(7, ge=1, le=90)) -> HTMLResponse:
    data = analytics_data(days=days)
    return templates.TemplateResponse(
        request, "observability/analytics.html",
        {"data": data, "days": days},
    )


@router.get("/tokens", response_class=HTMLResponse)
async def tokens(request: Request) -> HTMLResponse:
    results = run_checks()
    return templates.TemplateResponse(
        request, "observability/tokens.html",
        {"results": results},
    )


@router.get("/logs/{name}", response_class=HTMLResponse)
async def logs_page(request: Request, name: str) -> HTMLResponse:
    if name not in daemon_control.DAEMONS:
        raise HTTPException(404, f"unknown daemon: {name}")
    return templates.TemplateResponse(
        request, "observability/logs.html",
        {"name": name, "daemons": list(daemon_control.DAEMONS.keys())},
    )
