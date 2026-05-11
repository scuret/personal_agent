"""Facts (memory) viewer — read-only for Phase 1."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from memory.store import MemoryStore
from web.templating import templates

router = APIRouter()


@router.get("/facts", response_class=HTMLResponse)
async def list_facts(request: Request) -> HTMLResponse:
    store = MemoryStore()
    facts = store.recall_facts(limit=200)
    by_category: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        by_category[f.get("category") or "(uncategorized)"].append(f)
    return templates.TemplateResponse(
        request, "facts/list.html",
        {
            "by_category": dict(sorted(by_category.items())),
            "total": len(facts),
        },
    )
