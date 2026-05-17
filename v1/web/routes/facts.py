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
    # Review queue — facts created in the last 24h, regardless of
    # category. Security batch 5 F2: the agent only logs facts during
    # interactive chat (automated triggers are blocked), so this view
    # surfaces "what did the agent learn recently" so the principal can
    # spot anything that came from a chat where they didn't actually
    # mean to teach the agent something (e.g. an injection lure inside
    # an email body that the agent partially fell for).
    recent = store.recent_facts(hours=24, limit=50)
    return templates.TemplateResponse(
        request, "facts/list.html",
        {
            "by_category": _grouped(facts),
            "total": len(facts),
            "recent": recent,
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
