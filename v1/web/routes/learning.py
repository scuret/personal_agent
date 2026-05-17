"""/learning — inspect + curate the agent's per-trigger feedback bank.

Surfaces the `trigger_examples` table (written by the learning MCP
server from chat) so the user can:
  * See what corrections they've recorded for each trigger
  * Soft-delete a stale example so it stops influencing the prompt
  * Browse archived examples for an audit trail

The chat-side flow is the primary creation surface — this page is for
verification + cleanup. No create form here on purpose; corrections
should come with conversational context (the agent fills in
input_payload + expected_output + note for you).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from memory.store import MemoryStore
from web.templating import templates

router = APIRouter(prefix="/learning")


@router.get("", response_class=HTMLResponse)
async def index(request: Request, archived: int = 0) -> HTMLResponse:
    store = MemoryStore()
    show_archived = bool(archived)

    triggers: list[dict] = []
    for name in store.TRIGGER_NAMES:
        active_count = store.count_trigger_examples(name, active_only=True)
        total_count = store.count_trigger_examples(name, active_only=False)
        examples = store.list_trigger_examples(
            trigger_name=name,
            limit=50,
            active_only=not show_archived,
        )
        triggers.append({
            "name": name,
            "active_count": active_count,
            "total_count": total_count,
            "archived_count": total_count - active_count,
            "examples": examples,
        })

    return templates.TemplateResponse(
        request,
        "learning/index.html",
        {
            "triggers": triggers,
            "show_archived": show_archived,
        },
    )


@router.post("/{example_id}/archive")
async def archive_example(example_id: int) -> RedirectResponse:
    """Soft-delete one example. Used by the per-row archive button."""
    store = MemoryStore()
    ok = store.soft_delete_trigger_example(example_id)
    if not ok:
        # 404 keeps the contract honest if the row was already archived
        # or never existed.
        raise HTTPException(status_code=404, detail="example not found or already archived")
    return RedirectResponse(url="/learning", status_code=303)
