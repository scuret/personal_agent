"""Facts (memory) viewer — list + create + deactivate."""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from memory.store import MemoryStore
from web.templating import templates

router = APIRouter()


def _grouped(facts: list[dict]) -> dict[str, list[dict]]:
    by_category: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        by_category[f.get("category") or "(uncategorized)"].append(f)
    return dict(sorted(by_category.items()))


@router.get("/facts", response_class=HTMLResponse)
async def list_facts(request: Request) -> HTMLResponse:
    store = MemoryStore()
    facts = store.recall_facts(limit=200)
    return templates.TemplateResponse(
        request, "facts/list.html",
        {
            "by_category": _grouped(facts),
            "total": len(facts),
        },
    )


@router.post("/facts", response_class=HTMLResponse)
async def create_fact(
    request: Request,
    content: str = Form(...),
    category: str = Form("note"),
    confidence: float = Form(1.0),
) -> HTMLResponse:
    """Create a new fact from the web UI. Same write path the agent uses."""
    text = content.strip()
    cat = (category or "note").strip() or "note"
    if not text:
        raise HTTPException(400, "fact content is required")
    store = MemoryStore()
    store.log_fact(content=text, category=cat, confidence=float(confidence))
    return RedirectResponse("/facts", status_code=303)


@router.post("/facts/{fact_id}/deactivate")
async def deactivate_fact(fact_id: int) -> RedirectResponse:
    """Soft-delete a fact (sets is_active=0)."""
    store = MemoryStore()
    store.deactivate_fact(fact_id)
    return RedirectResponse("/facts", status_code=303)
