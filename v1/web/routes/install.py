"""First-run install entrypoint.

`/install` is now a thin redirect into the guided web wizard at
`/wizard`. The wizard reads `.env` + `data/.install_progress.json` to
decide which step you're on; this route just sends you there.

If the wizard's already been completed (`.install_progress.json` has
`completed_at`), `/install` redirects to the dashboard instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from core.paths import env_example_path, env_path, install_progress_path

router = APIRouter()

# Snapshot at import.
ENV_PATH = env_path()
ENV_EXAMPLE_PATH = env_example_path()
PROGRESS_PATH = install_progress_path()


def _required_key_present() -> bool:
    if not ENV_PATH.exists():
        return False
    parsed = dotenv_values(ENV_PATH) or {}
    return bool((parsed.get("ANTHROPIC_API_KEY") or "").strip())


def _wizard_completed() -> bool:
    if not PROGRESS_PATH.exists():
        return False
    try:
        return bool(json.loads(PROGRESS_PATH.read_text()).get("completed_at"))
    except (json.JSONDecodeError, OSError):
        return False


@router.get("/install", response_model=None)
async def install_entry() -> RedirectResponse:
    """Route to /wizard for first-run or in-progress, to / once complete."""
    if _wizard_completed() and _required_key_present():
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/wizard", status_code=303)


@router.post("/install/bootstrap")
async def bootstrap_env() -> RedirectResponse:
    """Legacy endpoint kept for backwards compat. Creates .env from
    .env.example then redirects to the wizard."""
    if not ENV_PATH.exists() and ENV_EXAMPLE_PATH.exists():
        ENV_PATH.write_text(ENV_EXAMPLE_PATH.read_text())
        ENV_PATH.chmod(0o600)
    return RedirectResponse("/wizard", status_code=303)
