"""Web chat surface — SSE-streamed responses, conversation continuity."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

from agent_host import process_turn_stream
from memory.store import MemoryStore
from web.sessions import POOL
from web.templating import templates

router = APIRouter()

# Source tag for web-originated conversations. Continuity uses
# resume_or_open_conversation('web') with a 4h gap (same as relays).
WEB_SOURCE = "web"
CONVERSATION_GAP_HOURS = 4.0

V1_DIR = Path(__file__).resolve().parent.parent.parent
UPLOADS_DIR = V1_DIR / "data" / "uploads"

# Browser uploads are capped here to keep the SSE URL well under typical
# query-string limits when the [attachment: ...] markers are encoded
# into it. iMessage's own typical limit is comparable.
MAX_IMAGES_PER_TURN = 4
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/heic", "image/heif", "image/webp", "image/gif"}

# Total cap on the data/uploads/ tree. Once exceeded, oldest
# conversation-uploads directories get deleted FIFO until under the
# cap (see _enforce_uploads_cap below). Default 500MB, override via
# UPLOADS_TOTAL_CAP_MB. Set to 0 to disable. ROADMAP M5.
_UPLOADS_CAP_MB_DEFAULT = 500

# FastAPI Form/File markers need to be evaluated at module import time
# (B008: don't call functions in argument defaults) — hoist them here.
_IMAGES_FIELD = File(default=[])


def _store() -> MemoryStore:
    return MemoryStore()


def _purge_uploads_for(conv_id: str) -> None:
    """Delete data/uploads/<conv_id>/ tree, ignoring missing dirs.
    Called when a conversation closes so sensitive images don't
    accumulate. ROADMAP M5.
    """
    target = UPLOADS_DIR / conv_id
    if not target.exists() or not target.is_dir():
        return
    try:
        shutil.rmtree(target)
    except OSError as e:
        print(f"[chat] failed to purge uploads for {conv_id}: {e}")


def _uploads_cap_bytes() -> int:
    """Return the configured upload-tree cap in bytes, or 0 if disabled."""
    raw = (os.environ.get("UPLOADS_TOTAL_CAP_MB") or "").strip()
    if not raw:
        return _UPLOADS_CAP_MB_DEFAULT * 1024 * 1024
    try:
        mb = int(raw)
    except ValueError:
        return _UPLOADS_CAP_MB_DEFAULT * 1024 * 1024
    if mb <= 0:
        return 0
    return mb * 1024 * 1024


def _enforce_uploads_cap() -> None:
    """Walk data/uploads/, sum sizes, and FIFO-delete oldest conv dirs
    until under the cap. Cap of 0 disables. Cheap to call on every
    send because the typical tree is tiny. ROADMAP M5."""
    cap = _uploads_cap_bytes()
    if cap <= 0 or not UPLOADS_DIR.exists():
        return
    conv_dirs: list[tuple[float, Path, int]] = []
    total = 0
    for child in UPLOADS_DIR.iterdir():
        if not child.is_dir():
            continue
        size = 0
        oldest_mtime = float("inf")
        for f in child.rglob("*"):
            if f.is_file():
                try:
                    st = f.stat()
                except OSError:
                    continue
                size += st.st_size
                oldest_mtime = min(oldest_mtime, st.st_mtime)
        conv_dirs.append((oldest_mtime, child, size))
        total += size
    if total <= cap:
        return
    # Oldest first. Stop deleting once we're back under the cap.
    conv_dirs.sort(key=lambda t: t[0])
    for _mtime, path, size in conv_dirs:
        if total <= cap:
            break
        try:
            shutil.rmtree(path)
            print(f"[chat] uploads cap exceeded — purged {path.name} ({size} bytes)")
            total -= size
        except OSError as e:
            print(f"[chat] cap-purge failed on {path}: {e}")


def _ext_from_mime(mime: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".bin")


@router.get("/chat", response_class=HTMLResponse)
async def chat_shell(request: Request) -> HTMLResponse:
    """Chat page — resumes the most recent web conversation if one's
    still within the gap window, otherwise opens a fresh one."""
    store = _store()
    conv_id = store.resume_or_open_conversation(
        source=WEB_SOURCE, gap_threshold_hours=CONVERSATION_GAP_HOURS
    )
    # Load history for this conversation.
    rows = store._conn().execute(
        "SELECT role, content, tool_calls, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()
    messages = []
    for r in rows:
        d = dict(r)
        if d.get("tool_calls"):
            try:
                d["tool_calls_parsed"] = json.loads(d["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                d["tool_calls_parsed"] = None
        messages.append(d)
    return templates.TemplateResponse(
        request, "chat.html",
        {"conversation_id": conv_id, "messages": messages},
    )


@router.post("/chat/{conv_id}/send", response_class=HTMLResponse)
async def send_message(
    request: Request,
    conv_id: str,
    text: str = Form(""),
    images: list[UploadFile] = _IMAGES_FIELD,
) -> HTMLResponse:
    """Append the user's bubble immediately + return a placeholder
    assistant bubble that will be filled via SSE.

    Accepts optional image attachments. When images are attached, they
    are saved under data/uploads/{conv_id}/ and prepended to the agent-
    facing text as [attachment: image at PATH (mime)] markers — same
    convention as the iMessage / Telegram / Discord / Slack relays so
    the vision sub-agent works identically in every surface.
    """
    caption = (text or "").strip()
    valid_images: list[UploadFile] = [f for f in images if f and f.filename]
    if not caption and not valid_images:
        raise HTTPException(400, "empty message")
    if len(valid_images) > MAX_IMAGES_PER_TURN:
        raise HTTPException(
            400, f"too many images (max {MAX_IMAGES_PER_TURN} per turn)"
        )

    conv_uploads = UPLOADS_DIR / conv_id
    saved: list[dict[str, str]] = []
    for f in valid_images:
        mime = (f.content_type or "").lower()
        if mime not in ALLOWED_IMAGE_MIMES:
            raise HTTPException(400, f"unsupported image type: {mime!r}")
        conv_uploads.mkdir(parents=True, exist_ok=True)
        dest = conv_uploads / f"{uuid.uuid4().hex}{_ext_from_mime(mime)}"
        dest.write_bytes(await f.read())
        saved.append({
            "path": str(dest),
            "mime": mime,
            "url": f"/uploads/{conv_id}/{dest.name}",
        })

    if saved:
        marker_block = "\n".join(
            f"[attachment: image at {a['path']} ({a['mime']})]" for a in saved
        )
        body = caption if caption else "(no caption)"
        agent_text = f"{marker_block}\n{body}"
        # Keep the upload tree bounded so sensitive images don't pile
        # up forever. ROADMAP M5.
        _enforce_uploads_cap()
    else:
        agent_text = caption

    # The actual store.append_message + agent call happens in /stream;
    # this just renders the optimistic UI. The pending bubble passes
    # `agent_text` (markers + caption) into the SSE URL; the user-
    # facing bubble shows only `caption` + thumbnails of `images`.
    return templates.TemplateResponse(
        request, "_chat_pending.html",
        {
            "conversation_id": conv_id,
            "user_caption": caption,
            "user_images": saved,
            "agent_text": agent_text,
            "now": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )


@router.get("/chat/{conv_id}/stream")
async def stream(conv_id: str, text: str):
    """SSE: run the user's turn against the live SDK client and stream
    each text chunk as an `event: text` SSE frame. Browser-side, the
    chat-pending bubble subscribes and appends each chunk."""
    store = _store()

    async def event_source():
        try:
            client = await POOL.get(conv_id, store)
        except Exception as e:
            yield {"event": "error", "data": f"client startup failed: {e}"}
            return
        try:
            async for chunk in process_turn_stream(client, store, conv_id, text):
                kind = chunk.get("event")
                if kind == "text":
                    yield {"event": "text", "data": chunk.get("text", "")}
                elif kind == "tool":
                    yield {"event": "tool", "data": chunk.get("name", "")}
                elif kind == "done":
                    cost = chunk.get("cost_usd")
                    dur = chunk.get("duration_ms")
                    yield {
                        "event": "done",
                        "data": json.dumps({"cost_usd": cost, "duration_ms": dur}),
                    }
        except asyncio.CancelledError:
            # Browser closed the connection mid-stream; nothing to clean up.
            return
        except Exception as e:
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(event_source())


@router.post("/chat/{conv_id}/end")
async def end_conversation(conv_id: str) -> dict:
    """Close the current conversation explicitly — next /chat opens a
    fresh one. Closes the pooled client too AND purges any image
    attachments the user dropped into this conversation (ROADMAP M5).
    """
    store = _store()
    store.close_conversation(conv_id)
    await POOL.close(conv_id)
    _purge_uploads_for(conv_id)
    return {"ok": True}
