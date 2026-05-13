"""First-run install entrypoint.

`/install` detects a fresh checkout (no `.env`, or `ANTHROPIC_API_KEY` is
empty) and walks the user into the settings page with a first-run banner.
If `.env` already has the required key, this just redirects to the
dashboard — the install flow is a one-time guide, not a permanent page.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.templating import templates

router = APIRouter()

V1_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = V1_DIR / ".env"
ENV_EXAMPLE_PATH = V1_DIR / ".env.example"


def _required_key_present() -> bool:
    """True when .env exists and ANTHROPIC_API_KEY is non-empty."""
    if not ENV_PATH.exists():
        return False
    parsed = dotenv_values(ENV_PATH) or {}
    return bool((parsed.get("ANTHROPIC_API_KEY") or "").strip())


@router.get("/install", response_class=HTMLResponse, response_model=None)
async def install_entry(request: Request) -> HTMLResponse | RedirectResponse:
    """If already installed, send the user to the dashboard. Otherwise
    render the welcome page that copies .env.example → .env (if missing)
    and links straight into /settings?first_run=1."""
    if _required_key_present():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "install/welcome.html",
        {
            "env_exists": ENV_PATH.exists(),
            "env_example_exists": ENV_EXAMPLE_PATH.exists(),
        },
    )


@router.post("/install/bootstrap")
async def bootstrap_env() -> RedirectResponse:
    """Create .env from .env.example so the user can edit it in /config/env."""
    if not ENV_PATH.exists() and ENV_EXAMPLE_PATH.exists():
        ENV_PATH.write_text(ENV_EXAMPLE_PATH.read_text())
        ENV_PATH.chmod(0o600)
    return RedirectResponse("/settings?first_run=1", status_code=303)
