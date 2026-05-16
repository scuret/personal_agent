"""Guided install wizard at `/wizard`.

Linear N-step flow that walks a non-technical user through every
required + optional configuration choice. Each step has a "what
you'll do" + "why it matters" header, a form, a link to the matching
`SETUP.md#<anchor>` section for the click-by-click provider
walkthrough, and (where applicable) a "Verify" button that
SSE-streams `--check` output.

State is derived from `.env` + filesystem (token files, credentials,
schema) + a small `data/.install_progress.json` marker that holds
the non-derivable bits (which optional sub-agents the user enabled,
how far they've walked the configure loop, whether they
acknowledged the privacy disclosure).

The wizard reuses existing infrastructure rather than duplicating:
  * `SUBAGENTS` (tools/install.py) — sub-agent metadata
  * `TRANSPORTS` (web/routes/settings_transports.py) — transport metadata
  * `_AUTH_SCRIPTS` / `_TOKEN_FILES` / `start_connect` / `stream_connect`
    (web/routes/settings.py) — OAuth subprocess + SSE
  * `_is_sensitive` (web/routes/config.py) — credential masking
  * `_env_io.write_env_values` (web/routes/_env_io.py) — env writer
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from tools.install import SUBAGENTS
from web.routes._env_io import ENV_PATH, read_env_dict, write_env_values
from web.routes.settings import _AUTH_SCRIPTS, _TOKEN_FILES
from web.routes.settings_transports import _TRANSPORT_BY_NAME, TRANSPORTS
from web.templating import templates

router = APIRouter(prefix="/wizard")

V1_DIR = Path(__file__).resolve().parent.parent.parent
PROGRESS_PATH = V1_DIR / "data" / ".install_progress.json"
CONFIG_DIR = V1_DIR / "config"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"


# ─── Progress state ────────────────────────────────────────────────────────


def _read_progress() -> dict[str, Any]:
    if not PROGRESS_PATH.exists():
        return {
            "subagents_enabled": [],
            "subagents_configure_index": 0,
            "acknowledged_disclosure": False,
            "skipped_steps": [],
            "completed_at": None,
        }
    try:
        return json.loads(PROGRESS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            "subagents_enabled": [],
            "subagents_configure_index": 0,
            "acknowledged_disclosure": False,
            "skipped_steps": [],
            "completed_at": None,
        }


def _write_progress(updates: dict[str, Any]) -> dict[str, Any]:
    state = _read_progress()
    state.update(updates)
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(state, indent=2))
    with contextlib.suppress(OSError):
        os.chmod(PROGRESS_PATH, 0o600)
    return state


# ─── Step definitions ──────────────────────────────────────────────────────


@dataclass
class WizardStep:
    name: str               # URL slug
    title: str              # human-facing title
    blurb_what: str         # "what you'll do" paragraph
    blurb_why: str          # "why it matters" paragraph
    setup_anchor: str       # SETUP.md anchor (without #)
    is_complete: Callable[[dict[str, str], dict[str, Any]], bool]
    is_relevant: Callable[[dict[str, str], dict[str, Any]], bool] = lambda env, p: True
    mandatory: bool = False


def _subagents_enabled(progress: dict[str, Any]) -> list[str]:
    return list(progress.get("subagents_enabled") or [])


def _any_google_subagent_enabled(progress: dict[str, Any]) -> bool:
    enabled = set(_subagents_enabled(progress))
    return bool(enabled & {"gmail", "calendar", "drive", "docs", "sheets"})


def _gmail_enabled(env: dict[str, str], progress: dict[str, Any]) -> bool:
    return "gmail" in _subagents_enabled(progress)


def _transport_has_required_fields(env: dict[str, str], transport_name: str) -> bool:
    t = _TRANSPORT_BY_NAME.get(transport_name)
    if not t:
        return False
    for f in t.fields:
        if not f.optional and not (env.get(f.key) or "").strip():
            # `select` fields with a default count as set.
            if f.select_options and f.default:
                continue
            return False
    return True


STEPS: list[WizardStep] = [
    WizardStep(
        name="welcome",
        title="Before you start",
        blurb_what="Read the privacy / cost / threat-model disclosure and acknowledge.",
        blurb_why="This is a single-user, local-first tool that sends a lot of personal data to Anthropic. Worth knowing what you're signing up for.",
        setup_anchor="welcome",
        is_complete=lambda env, p: bool(p.get("acknowledged_disclosure")),
        mandatory=True,
    ),
    WizardStep(
        name="anthropic",
        title="Anthropic API key",
        blurb_what="Sign up at console.anthropic.com, generate an API key, paste it in.",
        blurb_why="Without an API key the agent can't reach Claude. This is the only universally-required piece.",
        setup_anchor="anthropic",
        is_complete=lambda env, p: bool((env.get("ANTHROPIC_API_KEY") or "").strip()),
        mandatory=True,
    ),
    WizardStep(
        name="transport_pick",
        title="Pick a transport",
        blurb_what="Choose how the agent talks to you: iMessage, Telegram, Discord, Slack, or SMS via Twilio.",
        blurb_why="One transport runs at a time. You can switch later by re-running this wizard.",
        setup_anchor="pick-transport",
        is_complete=lambda env, p: bool((env.get("RELAY_TRANSPORT") or "").strip()),
        mandatory=True,
    ),
    WizardStep(
        name="transport_config",
        title="Configure your transport",
        blurb_what="Fill in the credentials for the transport you picked. Each transport has a Verify button that confirms your setup before you continue.",
        blurb_why="Without the credentials, the relay daemon won't be able to connect to the chosen provider.",
        setup_anchor="pick-transport",
        is_complete=lambda env, p: _transport_has_required_fields(
            env, (env.get("RELAY_TRANSPORT") or "").strip()
        ),
        mandatory=True,
    ),
    WizardStep(
        name="subagents_pick",
        title="Pick optional sub-agents",
        blurb_what="Toggle which integrations you want — Gmail, Calendar, Todoist, Notion, Spotify, etc. Each one tells you what you can do with it.",
        blurb_why="Sub-agents are how the agent reaches into specific services. Enable the ones that match your daily use; skip the rest. You can come back later.",
        setup_anchor="sub-agents",
        is_complete=lambda env, p: "subagents_pick" in (p.get("skipped_steps") or [])
                                   or bool(p.get("subagents_enabled") is not None
                                           and "subagents_enabled" in p),
        mandatory=False,
    ),
    WizardStep(
        name="subagents_configure",
        title="Configure enabled sub-agents",
        blurb_what="One screen per sub-agent you enabled. Paste in API keys, click Connect for OAuth, click Verify to confirm.",
        blurb_why="Each sub-agent needs its own auth before the agent can use it. Skip per-sub-agent allowed — you can finish later from /settings.",
        setup_anchor="sub-agents",
        is_complete=lambda env, p: (
            p.get("subagents_configure_index", 0) >= len(_subagents_enabled(p))
        ),
        is_relevant=lambda env, p: bool(_subagents_enabled(p)),
        mandatory=False,
    ),
    WizardStep(
        name="google_oauth",
        title="Google OAuth",
        blurb_what="Upload the OAuth client JSON from Google Cloud Console, then click Run OAuth to grant access in your browser.",
        blurb_why="Gmail, Calendar, Drive, Docs, and Sheets all share one Google OAuth flow. Only needed if you enabled at least one of those.",
        setup_anchor="google-cloud-project",
        is_complete=lambda env, p: (V1_DIR / "data" / "google_token.pickle").exists(),
        is_relevant=lambda env, p: _any_google_subagent_enabled(p),
        mandatory=False,
    ),
    WizardStep(
        name="triggers_email",
        title="Email triage",
        blurb_what="Configure how the scheduler decides which emails are worth pinging your phone about.",
        blurb_why="Every non-automated unread Gmail message gets a Haiku triage call when this is on. Set EMAIL_TRIAGE_LOCAL_ONLY=true to skip Anthropic entirely.",
        setup_anchor="triggers-email",
        is_complete=lambda env, p: "triggers_email" in (p.get("skipped_steps") or [])
                                   or (CONFIG_DIR / "triggers.yaml").exists(),
        is_relevant=_gmail_enabled,
        mandatory=False,
    ),
    WizardStep(
        name="triggers_schedule",
        title="Briefs + reminders",
        blurb_what="Pick when the morning brief fires and when the Sunday weekly review runs.",
        blurb_why="These two are the daily/weekly touchpoints with the agent. Default 07:30 + Sun 20:00 work for most people.",
        setup_anchor="triggers-email",
        is_complete=lambda env, p: "triggers_schedule" in (p.get("skipped_steps") or [])
                                   or (CONFIG_DIR / "triggers.yaml").exists(),
        mandatory=False,
    ),
    WizardStep(
        name="behavior",
        title="Behavior defaults",
        blurb_what="Set your timezone (used for scheduling) and Claude model (Sonnet is the default).",
        blurb_why="The scheduler fires briefs at the right local time and uses Opus for briefs / Haiku for triage regardless of your default.",
        setup_anchor="behavior",
        is_complete=lambda env, p: bool((env.get("USER_TIMEZONE") or "").strip()),
        mandatory=False,
    ),
    WizardStep(
        name="launchagents",
        title="Install LaunchAgents",
        blurb_what="One button — installs four LaunchAgents (relay, scheduler, log-rotation, webui) that auto-start on login.",
        blurb_why="Without this step the agent doesn't run unless you manually launch each daemon every time. The button runs ./launch_agents/install.sh.",
        setup_anchor="launchagents",
        is_complete=lambda env, p: "launchagents" in (p.get("skipped_steps") or []),
        mandatory=True,
    ),
    WizardStep(
        name="done",
        title="All done",
        blurb_what="Quick health-check + links to the dashboard, settings, and chat.",
        blurb_why="Marks the wizard complete so home stops redirecting here.",
        setup_anchor="welcome",
        is_complete=lambda env, p: bool(p.get("completed_at")),
        mandatory=False,
    ),
]


_STEP_BY_NAME = {s.name: s for s in STEPS}


def _next_step(current: str, env: dict[str, str], progress: dict[str, Any]) -> str:
    """Return the name of the next relevant step after `current`."""
    idx = next((i for i, s in enumerate(STEPS) if s.name == current), -1)
    for s in STEPS[idx + 1 :]:
        if s.is_relevant(env, progress):
            return s.name
    return "done"


def _earliest_unmet_step(env: dict[str, str], progress: dict[str, Any]) -> str:
    # Fast path for "this install was already configured before the
    # wizard existed" — if the user has an ANTHROPIC_API_KEY in .env
    # AND never opened the wizard before, they implicitly already
    # made the cost/privacy decisions when they installed. Skip
    # straight to done so we don't force them to re-acknowledge.
    if (env.get("ANTHROPIC_API_KEY") or "").strip() and not progress.get(
        "acknowledged_disclosure"
    ):
        return "done"

    for s in STEPS:
        if not s.is_relevant(env, progress):
            continue
        if not s.is_complete(env, progress) and s.name not in (
            progress.get("skipped_steps") or []
        ):
            return s.name
    return "done"


def _progress_bar(env: dict[str, str], progress: dict[str, Any], current: str) -> list[dict]:
    """Build the progress bar for the layout template — one dot per
    relevant step with completion / current state."""
    out: list[dict] = []
    for s in STEPS:
        if not s.is_relevant(env, progress):
            continue
        out.append({
            "name": s.name,
            "title": s.title,
            "complete": s.is_complete(env, progress),
            "skipped": s.name in (progress.get("skipped_steps") or []),
            "current": s.name == current,
        })
    return out


# ─── Route helpers ─────────────────────────────────────────────────────────


def _step_context(step: WizardStep, env: dict[str, str], progress: dict[str, Any]) -> dict:
    """Common context for all wizard templates."""
    return {
        "step": step,
        "progress_bar": _progress_bar(env, progress, step.name),
        "env": env,
        "progress": progress,
    }


# ─── Routes ────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse, response_model=None)
async def index(request: Request):
    """Redirect to the earliest unmet step."""
    env = read_env_dict()
    progress = _read_progress()
    if progress.get("completed_at"):
        return RedirectResponse("/", status_code=303)
    target = _earliest_unmet_step(env, progress)
    return RedirectResponse(f"/wizard/{target}", status_code=303)


@router.get("/{step_name}", response_class=HTMLResponse, response_model=None)
async def step(request: Request, step_name: str):
    step_def = _STEP_BY_NAME.get(step_name)
    if not step_def:
        raise HTTPException(404, f"unknown step: {step_name!r}")
    env = read_env_dict()
    progress = _read_progress()

    # Per-step extra context.
    ctx = _step_context(step_def, env, progress)

    if step_name == "transport_pick":
        ctx["transports"] = TRANSPORTS
        ctx["current_transport"] = (env.get("RELAY_TRANSPORT") or "").strip()
    elif step_name == "transport_config":
        name = (env.get("RELAY_TRANSPORT") or "").strip()
        if not name:
            return RedirectResponse("/wizard/transport_pick", status_code=303)
        t = _TRANSPORT_BY_NAME.get(name)
        if not t:
            return RedirectResponse("/wizard/transport_pick", status_code=303)
        ctx["transport"] = t
        ctx["fields"] = [
            {
                "key": f.key,
                "label": f.label,
                "help": f.help,
                "secret": f.secret,
                "optional": f.optional,
                "placeholder": f.placeholder,
                "default": f.default,
                "select_options": f.select_options,
                "value": env.get(f.key, ""),
            }
            for f in t.fields
        ]
    elif step_name == "subagents_pick":
        ctx["always_on"] = [s for s in SUBAGENTS if s.always_on]
        ctx["optional"] = [s for s in SUBAGENTS if not s.always_on]
        ctx["enabled"] = set(progress.get("subagents_enabled") or [])
    elif step_name == "subagents_configure":
        enabled = _subagents_enabled(progress)
        if not enabled:
            return RedirectResponse(
                f"/wizard/{_next_step(step_name, env, progress)}", status_code=303
            )
        i = int(request.query_params.get("i") or progress.get("subagents_configure_index") or 0)
        if i >= len(enabled):
            # Loop finished.
            return RedirectResponse(
                f"/wizard/{_next_step(step_name, env, progress)}", status_code=303
            )
        sa = next((s for s in SUBAGENTS if s.name == enabled[i]), None)
        if not sa:
            # Stale entry — skip it.
            return RedirectResponse(f"/wizard/subagents_configure?i={i + 1}", status_code=303)
        ctx["sa"] = sa
        ctx["index"] = i
        ctx["total"] = len(enabled)
        ctx["values"] = {var: env.get(var, "") for var in sa.env_vars}
        ctx["has_oauth"] = sa.name in _AUTH_SCRIPTS
        # Connect / token-cached state for the "this sub-agent looks
        # already connected" badge.
        token_files = _TOKEN_FILES.get(sa.name, [])
        ctx["tokens_present"] = all(p.exists() for p in token_files) if token_files else False
    elif step_name == "google_oauth":
        ctx["credentials_present"] = CREDENTIALS_PATH.exists()
        ctx["token_present"] = (V1_DIR / "data" / "google_token.pickle").exists()
    elif step_name == "done":
        progress = _write_progress({"completed_at": ctx["progress"].get("completed_at") or _now()})
        ctx["progress"] = progress

    return templates.TemplateResponse(request, f"wizard/{step_name}.html", ctx)


@router.post("/{step_name}", response_model=None)
async def step_save(request: Request, step_name: str):
    """Persist this step's form, then redirect to the next."""
    step_def = _STEP_BY_NAME.get(step_name)
    if not step_def:
        raise HTTPException(404, f"unknown step: {step_name!r}")

    form = await request.form()
    env = read_env_dict()
    progress = _read_progress()

    if step_name == "welcome":
        if not form.get("acknowledge"):
            return RedirectResponse("/wizard/welcome?ack_required=1", status_code=303)
        if not ENV_PATH.exists():
            # First-run bootstrap: copy .env.example → .env if user
            # arrived without one.
            example = V1_DIR / ".env.example"
            if example.exists():
                ENV_PATH.write_text(example.read_text())
                os.chmod(ENV_PATH, 0o600)
        progress = _write_progress({"acknowledged_disclosure": True})

    elif step_name == "anthropic":
        key = (form.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            return RedirectResponse("/wizard/anthropic?empty=1", status_code=303)
        write_env_values({"ANTHROPIC_API_KEY": key})

    elif step_name == "transport_pick":
        name = (form.get("transport") or "").strip()
        if name not in _TRANSPORT_BY_NAME:
            raise HTTPException(400, f"unknown transport: {name!r}")
        write_env_values({"RELAY_TRANSPORT": name})

    elif step_name == "transport_config":
        name = (env.get("RELAY_TRANSPORT") or "").strip()
        t = _TRANSPORT_BY_NAME.get(name)
        if not t:
            return RedirectResponse("/wizard/transport_pick", status_code=303)
        updates: dict[str, str] = {}
        for f in t.fields:
            raw = form.get(f"field:{f.key}")
            if raw is None:
                continue
            updates[f.key] = str(raw).strip()
        if updates:
            write_env_values(updates)

    elif step_name == "subagents_pick":
        enabled = [v for v in form.getlist("subagent")]
        progress = _write_progress({
            "subagents_enabled": enabled,
            "subagents_configure_index": 0,
        })

    elif step_name == "subagents_configure":
        i = int(form.get("index") or progress.get("subagents_configure_index") or 0)
        sa_name = form.get("subagent") or ""
        sa = next((s for s in SUBAGENTS if s.name == sa_name), None)
        if sa:
            updates = {}
            for var in sa.env_vars:
                raw = form.get(f"field:{var}")
                if raw is not None:
                    updates[var] = str(raw).strip()
            if updates:
                write_env_values(updates)
        next_index = i + 1
        progress = _write_progress({"subagents_configure_index": next_index})
        enabled = _subagents_enabled(progress)
        if next_index < len(enabled):
            return RedirectResponse(
                f"/wizard/subagents_configure?i={next_index}", status_code=303
            )

    elif step_name == "behavior":
        updates: dict[str, str] = {}
        for key in ("USER_TIMEZONE", "CLAUDE_MODEL"):
            raw = form.get(key)
            if raw is not None:
                updates[key] = str(raw).strip()
        if updates:
            write_env_values(updates)

    elif step_name == "launchagents":
        # The actual install runs via the existing /settings/launchagents/install
        # endpoint (SSE-streamed). This POST is just an acknowledgment that
        # the user has clicked through.
        progress = _write_progress(
            {"skipped_steps": list(set((progress.get("skipped_steps") or []) + ["launchagents"]))}
        )

    elif step_name in ("triggers_email", "triggers_schedule"):
        # v1 stub — the deeper YAML editing lives at /config/triggers.
        # The wizard just marks the step as visited so the user can
        # continue. Users who want to edit triggers click through to
        # /config/triggers from the in-step link.
        progress = _write_progress(
            {"skipped_steps": list(set((progress.get("skipped_steps") or []) + [step_name]))}
        )

    target = _next_step(step_name, read_env_dict(), progress)
    return RedirectResponse(f"/wizard/{target}", status_code=303)


@router.post("/{step_name}/skip", response_model=None)
async def step_skip(step_name: str):
    step_def = _STEP_BY_NAME.get(step_name)
    if not step_def:
        raise HTTPException(404, f"unknown step: {step_name!r}")
    if step_def.mandatory:
        raise HTTPException(400, f"step {step_name!r} is mandatory; cannot skip")
    progress = _write_progress({
        "skipped_steps": list(set((_read_progress().get("skipped_steps") or []) + [step_name])),
    })
    target = _next_step(step_name, read_env_dict(), progress)
    return RedirectResponse(f"/wizard/{target}", status_code=303)


# Hoisted to a module-level singleton so the function default isn't
# a fresh `File(...)` call on every request (ruff B008).
_FILE_REQUIRED = File(...)


@router.post("/google/credentials", response_model=None)
async def google_credentials(file: UploadFile = _FILE_REQUIRED):
    """Save the uploaded Google OAuth client JSON to config/credentials.json."""
    raw = await file.read()
    try:
        # Sanity-check it parses as JSON before writing.
        json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, "uploaded file is not valid JSON") from e
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_bytes(raw)
    os.chmod(CREDENTIALS_PATH, 0o600)
    return RedirectResponse("/wizard/google_oauth?uploaded=1", status_code=303)


@router.post("/finish", response_model=None)
async def finish():
    _write_progress({"completed_at": _now()})
    return RedirectResponse("/", status_code=303)


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")
