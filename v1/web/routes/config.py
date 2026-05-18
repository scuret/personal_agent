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

from core.paths import (
    env_example_path,
    env_path,
    personality_path,
    triggers_yaml_path,
)

router = APIRouter(prefix="/config")

# Snapshot at import.
ENV_PATH = env_path()
ENV_EXAMPLE_PATH = env_example_path()
TRIGGERS_PATH = triggers_yaml_path()
PERSONALITY_PATH = personality_path()


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


# Substrings in a var name that mark it as credential-shaped. Values
# matching these are rendered masked so a shoulder-surfer / screenshot /
# screen-share doesn't leak them.
_SECRET_HINTS = (
    "KEY", "SECRET", "TOKEN", "PASSWORD",
)

# PII denylist — variables that aren't strictly secrets but ARE
# personal data the user wouldn't want surfaced casually (home
# address, phone number, account IDs on third-party platforms). Same
# masking treatment as secrets. Matched both exactly and via the
# trailing-substring patterns (e.g. ``*_ALLOWED_USER_IDS``).
# ROADMAP "Security enhancements" M1.
_PII_EXACT = {
    "EIGHT_EMAIL",
    "TARGET_PHONE_NUMBER",
    "SELF_HANDLES",
    "USER_HOME_ADDRESS",
}
_PII_SUFFIXES = (
    "_ALLOWED_USER_IDS",
    "_ALLOWED_CHAT_IDS",
    "_BRIEF_CHAT_ID",
    "_BRIEF_RECIPIENT_ID",
    "_BRIEF_USER_ID",
)


def _is_sensitive(key: str) -> bool:
    """Return True if `key` is either credential-shaped or known PII."""
    upper = key.upper()
    if any(h in upper for h in _SECRET_HINTS):
        return True
    if upper in _PII_EXACT:
        return True
    return any(upper.endswith(s) for s in _PII_SUFFIXES)


def _keys_from(path: Path) -> set[str]:
    """Collect every KEY= from a dotenv file. Order-insensitive."""
    if not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            keys.add(line.partition("=")[0].strip())
    return keys


def _missing_from_example_block() -> list[dict]:
    """Return display lines for `.env.example` vars that the live `.env`
    doesn't yet have. Rendered as an "available" section at the top of
    the editor so the user can fill them in without re-running install.

    Each returned entry shares the same shape as `_read_env_lines`,
    just flagged with `is_missing: True` so the template can style /
    sort accordingly.
    """
    if not ENV_EXAMPLE_PATH.exists() or not ENV_PATH.exists():
        return []
    existing = _keys_from(ENV_PATH)
    out: list[dict] = []
    last_comment_block: list[str] = []
    for line in ENV_EXAMPLE_PATH.read_text().splitlines():
        s = line.strip()
        if not s:
            last_comment_block = []
            continue
        if s.startswith("#"):
            last_comment_block.append(line)
            continue
        if "=" in s:
            key = line.partition("=")[0].strip()
            if key in existing:
                last_comment_block = []
                continue
            out.append({
                "kind": "var",
                "key": key,
                "value": "",
                "masked_value": "",
                "is_secret": _is_sensitive(key),
                "is_missing": True,
                "hint": "\n".join(last_comment_block),
            })
            last_comment_block = []
    return out


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
            is_sensitive = _is_sensitive(key) and value
            # Mask all sensitive fields (both credentials AND PII).
            # Credentials show a 4-char prefix; PII shows nothing (the
            # prefix would still leak a phone area code or street name).
            if is_sensitive:
                upper = key.upper()
                is_credential = any(h in upper for h in _SECRET_HINTS)
                if is_credential and len(value) > 6:
                    masked = value[:4] + "…(masked)"
                else:
                    masked = "(masked)"
            else:
                masked = value
            out.append({
                "kind": "var",
                "key": key,
                "value": value,
                "masked_value": masked,
                "is_secret": is_sensitive,
                "is_missing": False,
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
            "missing_lines": _missing_from_example_block(),
            "saved": saved,
            "reveal": reveal,
        },
    )


@router.post("/env")
async def post_env(request: Request) -> RedirectResponse:
    """Re-emit .env from the form. Form fields are `var:KEY` for each
    variable value (preserving order), with empty values cleared.
    Comments + blank lines come from the canonical order in the form.

    Any `missing:KEY` field present in the form is treated as a new
    variable added from the .env.example surfacing block; if its value
    is non-empty it gets appended to .env with the matching comment
    block from .env.example so the live file stays self-documenting.
    """
    form = await request.form()
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
                new_lines.append(line)
            else:
                new_lines.append(f"{key}={new_value}")
        else:
            new_lines.append(line)

    # Append missing-from-example vars that the user filled in. Pull
    # their comment block from .env.example so the addition reads the
    # same as the rest of the file.
    missing = _missing_from_example_block()
    additions: list[str] = []
    for entry in missing:
        key = entry["key"]
        value = form.get(f"missing:{key}")
        if not value:
            # Empty input — only persist if user explicitly set it
            # (non-empty); otherwise leave .env untouched so the row
            # keeps surfacing on the next page load.
            continue
        if additions:
            additions.append("")
        if entry.get("hint"):
            additions.extend(entry["hint"].splitlines())
        additions.append(f"{key}={value}")
    if additions:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# ── Added from .env.example via /config/env ──")
        new_lines.extend(additions)

    ENV_PATH.write_text("\n".join(new_lines) + "\n")
    os.chmod(ENV_PATH, 0o600)
    return RedirectResponse("/config/env?saved=1", status_code=303)
