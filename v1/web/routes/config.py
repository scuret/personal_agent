"""Config editors — .env, triggers.yaml, personality.md.

Each editor reads the current file, lets the user edit it, writes back.
Cadence:
  - triggers.yaml: scheduler re-reads every 30s, no restart needed
  - personality.md: requires relay + scheduler restart
  - .env: requires full daemon restart (process startup only reads it)
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.templating import templates

router = APIRouter(prefix="/config")

V1_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = V1_DIR / ".env"
TRIGGERS_PATH = V1_DIR / "config" / "triggers.yaml"
PERSONALITY_PATH = V1_DIR / "config" / "personality.md"


@router.get("", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "config/index.html",
        {
            "env_exists": ENV_PATH.exists(),
            "triggers_exists": TRIGGERS_PATH.exists(),
            "personality_exists": PERSONALITY_PATH.exists(),
        },
    )


# ─── triggers.yaml ─────────────────────────────────────────────────────────


@router.get("/triggers", response_class=HTMLResponse)
async def get_triggers(request: Request, saved: bool = False) -> HTMLResponse:
    if not TRIGGERS_PATH.exists():
        raise HTTPException(404, f"triggers.yaml not found at {TRIGGERS_PATH}")
    content = TRIGGERS_PATH.read_text()
    return templates.TemplateResponse(
        request, "config/triggers.html",
        {"content": content, "saved": saved},
    )


@router.post("/triggers")
async def post_triggers(content: str = Form(...)) -> RedirectResponse:
    TRIGGERS_PATH.write_text(content)
    return RedirectResponse("/config/triggers?saved=1", status_code=303)


# ─── personality.md ────────────────────────────────────────────────────────


@router.get("/personality", response_class=HTMLResponse)
async def get_personality(request: Request, saved: bool = False) -> HTMLResponse:
    if not PERSONALITY_PATH.exists():
        raise HTTPException(404, f"personality.md not found at {PERSONALITY_PATH}")
    content = PERSONALITY_PATH.read_text()
    return templates.TemplateResponse(
        request, "config/personality.html",
        {"content": content, "saved": saved},
    )


@router.post("/personality")
async def post_personality(content: str = Form(...)) -> RedirectResponse:
    PERSONALITY_PATH.write_text(content)
    return RedirectResponse("/config/personality?saved=1", status_code=303)


# ─── .env ───────────────────────────────────────────────────────────────────


# Patterns that look like secret material; mask their values in the
# rendered editor to avoid casual shoulder-surfing.
_SECRET_HINTS = (
    "KEY", "SECRET", "TOKEN", "PASSWORD",
)


def _read_env_lines() -> list[dict]:
    """Parse .env into a list of {kind, key, value, masked_value, comment}
    entries. Preserves order and comments. The web UI renders this as
    a stack of editable rows."""
    out: list[dict] = []
    if not ENV_PATH.exists():
        return out
    for line in ENV_PATH.read_text().splitlines():
        s = line.strip()
        if not s:
            out.append({"kind": "blank"})
        elif s.startswith("#"):
            out.append({"kind": "comment", "text": line})
        elif "=" in s:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            is_secret = any(h in key for h in _SECRET_HINTS) and value
            masked = (value[:4] + "…(masked)") if (is_secret and len(value) > 6) else value
            out.append({
                "kind": "var",
                "key": key,
                "value": value,
                "masked_value": masked if is_secret else value,
                "is_secret": is_secret,
            })
        else:
            out.append({"kind": "raw", "text": line})
    return out


@router.get("/env", response_class=HTMLResponse)
async def get_env(request: Request, saved: bool = False, reveal: bool = False) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "config/env.html",
        {
            "lines": _read_env_lines(),
            "saved": saved,
            "reveal": reveal,
        },
    )


@router.post("/env")
async def post_env(request: Request) -> RedirectResponse:
    """Re-emit .env from the form. Form fields are `var:KEY` for each
    variable value (preserving order), with empty values cleared.
    Comments + blank lines come from the canonical order in the form."""
    form = await request.form()
    # Build a fresh file using the current structure: read existing lines,
    # for each `var` line, replace the value from the form; keep comments
    # and blanks as-is. (Adding new vars from the UI is a Phase 4 feature;
    # for now you can only edit existing ones.)
    if not ENV_PATH.exists():
        raise HTTPException(404, ".env not found — run install.sh first")

    new_lines: list[str] = []
    for line in ENV_PATH.read_text().splitlines():
        s = line.strip()
        if "=" in s and not s.startswith("#"):
            key, _, _ = line.partition("=")
            key = key.strip()
            field_name = f"var:{key}"
            new_value = form.get(field_name)
            if new_value is None:
                # Field wasn't in the form (browser or template glitch); preserve.
                new_lines.append(line)
            else:
                new_lines.append(f"{key}={new_value}")
        else:
            new_lines.append(line)
    ENV_PATH.write_text("\n".join(new_lines) + "\n")
    os.chmod(ENV_PATH, 0o600)
    return RedirectResponse("/config/env?saved=1", status_code=303)
