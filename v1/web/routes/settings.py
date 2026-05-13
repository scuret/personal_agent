"""Settings dashboard — sub-agent status, OAuth Connect buttons,
LaunchAgent install / restart, and links to the file editors under /config.

The Connect flow spawns the matching `mcp_servers/*_auth.py` script as a
subprocess and streams stdout back over SSE. The auth scripts open a
browser tab and listen on a fixed localhost port for the OAuth callback;
when the user grants access the script prints success and exits, the SSE
closes, and the page reloads to pick up the new token file.

LaunchAgent install/uninstall just shells the same scripts the CLI uses.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from tools.install import SUBAGENTS, SubAgent
from web import daemon_control
from web.templating import templates

router = APIRouter(prefix="/settings")

V1_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = V1_DIR / ".env"
DATA_DIR = V1_DIR / "data"
LAUNCH_AGENTS_DIR = V1_DIR / "launch_agents"

# Which auth script (under mcp_servers/) handles each sub-agent's OAuth
# step. Sub-agents not listed here are configured purely via .env (key
# pasted in) or are always-on with no auth.
_AUTH_SCRIPTS: dict[str, str] = {
    # The Google family shares a single OAuth pickle; any of them
    # triggers the same flow.
    "gmail":    "mcp_servers.google_auth",
    "calendar": "mcp_servers.google_auth",
    "drive":    "mcp_servers.google_auth",
    "docs":     "mcp_servers.google_auth",
    "sheets":   "mcp_servers.google_auth",
    "dropbox":  "mcp_servers.dropbox_auth",
    "spotify":  "mcp_servers.spotify_auth",
    "canva":    "mcp_servers.canva_auth",
    "linkedin": "mcp_servers.linkedin_auth",
}

# Where each sub-agent's cached token lives, if any. Used both for "is
# this connected" status and for the uninstall path. Sub-agents that
# only need a static API key (env var only, no OAuth) have no entry.
_TOKEN_FILES: dict[str, list[Path]] = {
    "gmail":      [DATA_DIR / "google_token.pickle"],
    "calendar":   [DATA_DIR / "google_token.pickle"],
    "drive":      [DATA_DIR / "google_token.pickle"],
    "docs":       [DATA_DIR / "google_token.pickle"],
    "sheets":     [DATA_DIR / "google_token.pickle"],
    "dropbox":    [DATA_DIR / "dropbox_token.json"],
    "spotify":    [DATA_DIR / "spotify_token.json"],
    "canva":      [DATA_DIR / "canva_token.json"],
    "linkedin":   [DATA_DIR / "linkedin_token.json"],
    "eightsleep": [DATA_DIR / "eight_token.json"],
}

# At most one auth subprocess at a time — they share localhost callback
# ports, so concurrent runs would collide.
_active_process: dict[str, asyncio.subprocess.Process] = {}


def _env() -> dict[str, str]:
    """Read .env without polluting os.environ (we want the on-disk
    value, not whatever the running daemon last loaded)."""
    if not ENV_PATH.exists():
        return {}
    parsed = dotenv_values(ENV_PATH) or {}
    return {k: (v or "") for k, v in parsed.items()}


def _status_for(sa: SubAgent, env: dict[str, str]) -> dict:
    """Compute the status row for a single sub-agent.

    Returns:
      configured: env vars present + non-empty (or always_on)
      connected:  token file exists if OAuth-based, else same as configured
      auth_script: which module to spawn for Connect (or None)
      token_files: which files to check / delete
    """
    if sa.always_on:
        return {
            "name": sa.name,
            "description": sa.description,
            "configured": True,
            "connected": True,
            "auth_script": None,
            "env_vars": sa.env_vars,
            "setup_url": sa.setup_url,
            "auth_help": sa.auth_help,
            "needs_google_oauth": False,
            "missing_env": [],
            "missing_tokens": [],
        }

    missing_env: list[str] = []
    for var in sa.env_vars:
        if not env.get(var):
            missing_env.append(var)
    configured_env = not missing_env

    tokens = _TOKEN_FILES.get(sa.name, [])
    missing_tokens = [str(p) for p in tokens if not p.exists()]

    if sa.needs_google_oauth:
        # Google sub-agents have no per-agent env var — the Google client
        # secret lives under config/credentials.json, not in .env. So
        # "configured" tracks whether credentials.json exists. The token
        # file presence tracks "connected."
        creds = V1_DIR / "config" / "credentials.json"
        configured_env = creds.exists()
        if not configured_env:
            missing_env = ["config/credentials.json (OAuth client JSON)"]

    needs_oauth = bool(tokens) or sa.needs_google_oauth
    connected = configured_env and (not needs_oauth or not missing_tokens)

    return {
        "name": sa.name,
        "description": sa.description,
        "configured": configured_env,
        "connected": connected,
        "auth_script": _AUTH_SCRIPTS.get(sa.name),
        "env_vars": sa.env_vars,
        "setup_url": sa.setup_url,
        "auth_help": sa.auth_help,
        "needs_google_oauth": sa.needs_google_oauth,
        "missing_env": missing_env,
        "missing_tokens": missing_tokens,
    }


def _all_status(env: dict[str, str]) -> list[dict]:
    return [_status_for(sa, env) for sa in SUBAGENTS]


def _launchagent_status() -> list[dict]:
    """Reuse the daemon_control snapshot for the four installed agents."""
    return daemon_control.status()  # type: ignore[return-value]


@router.get("", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    env = _env()
    return templates.TemplateResponse(
        request, "settings/index.html",
        {
            "subagents": _all_status(env),
            "daemons": _launchagent_status(),
            "env_present": ENV_PATH.exists(),
            "first_run": request.query_params.get("first_run") == "1",
            "active_runs": list(_active_process.keys()),
        },
    )


# ─── Connect (OAuth subprocess + SSE stream) ────────────────────────────────


@router.post("/connect/{name}")
async def start_connect(name: str) -> JSONResponse:
    """Spawn the auth subprocess for `name`. Returns a stream URL the
    page subscribes to via SSE to get live stdout."""
    script = _AUTH_SCRIPTS.get(name)
    if not script:
        raise HTTPException(404, f"no auth flow for sub-agent {name!r}")
    if name in _active_process and _active_process[name].returncode is None:
        return JSONResponse({"ok": True, "stream": f"/settings/connect/{name}/stream",
                             "note": "already running — re-attaching"})

    # Use the same venv python that's running this server.
    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", script,
        cwd=str(V1_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ},
    )
    _active_process[name] = proc
    return JSONResponse({"ok": True, "stream": f"/settings/connect/{name}/stream"})


@router.get("/connect/{name}/stream")
async def stream_connect(name: str):
    """SSE — yields each stdout line as `event: line`, then `event: done`
    with the exit code when the subprocess finishes."""
    proc = _active_process.get(name)
    if proc is None:
        raise HTTPException(404, f"no active auth run for {name!r}")

    async def events():
        try:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                yield {"event": "line", "data": raw.decode(errors="replace").rstrip()}
            await proc.wait()
            yield {"event": "done", "data": str(proc.returncode or 0)}
        finally:
            # Leave _active_process entry in place briefly so a quick
            # reconnect can still see "done"; clear it next time the
            # user starts a new run.
            if proc.returncode is not None:
                _active_process.pop(name, None)

    return EventSourceResponse(events())


@router.post("/connect/{name}/cancel")
async def cancel_connect(name: str) -> JSONResponse:
    proc = _active_process.get(name)
    if proc is None or proc.returncode is not None:
        return JSONResponse({"ok": True, "note": "no active run"})
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except TimeoutError:
        proc.kill()
    _active_process.pop(name, None)
    return JSONResponse({"ok": True})


# ─── LaunchAgent install / restart ─────────────────────────────────────────


@router.post("/launchagents/install")
async def install_launchagents() -> JSONResponse:
    """Run launch_agents/install.sh. Renders + loads the four plists."""
    script = LAUNCH_AGENTS_DIR / "install.sh"
    if not script.exists():
        raise HTTPException(500, f"installer not found: {script}")
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", str(script),
        cwd=str(V1_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out_bytes, _ = await proc.communicate()
    return JSONResponse({
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": out_bytes.decode(errors="replace"),
    })


@router.post("/launchagents/restart/{daemon}")
async def restart_daemon(daemon: str) -> JSONResponse:
    cfg = daemon_control.DAEMONS.get(daemon)
    if not cfg:
        raise HTTPException(404, f"unknown daemon: {daemon}")
    ok, err = daemon_control.restart(cfg["label"])
    return JSONResponse({"ok": ok, "error": err})
