"""Dedicated transport picker for the web UI.

`/settings/transports` lets the user pick which transport runs and
fill in only the env vars that transport needs — without scrolling
through the full `/config/env` editor. Each transport has a `Verify`
button that subprocess-runs the matching `python -m relay.<x>_relay
--check` and SSE-streams the output, so the user can confirm
everything works before flipping the master switch.

After saving:
  * `RELAY_TRANSPORT` plus the transport-specific fields are written
    to `.env`. The handler reuses the same comment-preserving
    append-or-edit logic as `/config/env`.
  * The relay daemon needs a restart to read the new env. We don't
    auto-restart here — the dashboard's daemon panel has the
    button, and the user usually wants to verify with `--check`
    first anyway.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from web.templating import templates

router = APIRouter(prefix="/settings/transports")

V1_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = V1_DIR / ".env"


# ─── Transport metadata ────────────────────────────────────────────────────


@dataclass
class TransportField:
    key: str
    label: str
    help: str = ""
    secret: bool = False
    placeholder: str = ""
    optional: bool = False
    select_options: list[str] = field(default_factory=list)
    default: str = ""


@dataclass
class TransportDef:
    name: str             # e.g. "imessage"
    label: str            # human-facing name
    description: str
    platform_note: str    # one-liner about macOS-only / cross-platform / etc.
    setup_url: str
    verify_module: str    # python -m <this> --check
    fields: list[TransportField]


# Mirror the SubAgent registry pattern from tools/install.py — one
# source of truth for transport metadata, consumed by both the GET
# render and the POST save.
TRANSPORTS: list[TransportDef] = [
    TransportDef(
        name="imessage",
        label="iMessage",
        description=(
            "Native iPhone integration via macOS Messages.app. Polls "
            "chat.db, sends via AppleScript. Requires Full Disk Access "
            "and Automation → Messages permissions for the Python "
            "binary running the daemon."
        ),
        platform_note="macOS only",
        setup_url="https://support.apple.com/guide/messages/welcome/mac",
        verify_module="relay.imessage_relay",
        fields=[
            TransportField(
                key="IMESSAGE_MODE",
                label="Mode",
                help="`self` = text yourself from your iPhone (note-to-self thread). `contact` = listen to one specific other person's messages.",
                select_options=["self", "contact"],
                default="self",
            ),
            TransportField(
                key="TARGET_PHONE_NUMBER",
                label="Target phone",
                help="In self mode: your own number (+15551234567). In contact mode: the other person's number or Apple-ID email.",
                placeholder="+15551234567",
                secret=True,
            ),
            TransportField(
                key="SELF_HANDLES",
                label="Self handles (optional)",
                help="Extra handles to watch alongside TARGET_PHONE_NUMBER, e.g. your Apple ID email. Comma-separated.",
                optional=True,
                secret=True,
            ),
            TransportField(
                key="IMESSAGE_POLL_INTERVAL",
                label="Poll interval (sec)",
                help="How often to poll chat.db. Lower = more responsive, more CPU.",
                default="5",
                optional=True,
            ),
            TransportField(
                key="IMESSAGE_GROUP_CHATS",
                label="Group chats (optional)",
                help="Comma-separated chat_identifier or display_name values to listen in alongside the primary 1:1 mode. Run `python -m relay.imessage_relay --check` to discover group IDs.",
                optional=True,
            ),
            TransportField(
                key="IMESSAGE_GROUP_TRIGGERS",
                label="Group triggers (optional)",
                help="Comma-separated substrings that summon the bot in group chats. Defaults to `@agent, hey agent, agent,` when blank.",
                optional=True,
            ),
        ],
    ),
    TransportDef(
        name="telegram",
        label="Telegram",
        description=(
            "Bot you create via @BotFather. Works from any host (Mac, "
            "Linux, Windows) — no chat.db dependency, no Apple ID "
            "needed. Long-polls via getUpdates."
        ),
        platform_note="cross-platform",
        setup_url="https://core.telegram.org/bots#how-do-i-create-a-bot",
        verify_module="relay.telegram_relay",
        fields=[
            TransportField(
                key="TELEGRAM_BOT_TOKEN",
                label="Bot token",
                help="From @BotFather after /newbot. Looks like `123456:ABC-DEF...`.",
                placeholder="123456:ABC-DEF...",
                secret=True,
            ),
            TransportField(
                key="TELEGRAM_ALLOWED_USER_IDS",
                label="Allowed Telegram user IDs",
                help="Comma-separated numeric IDs. Find yours via @userinfobot. Anyone NOT on this list is ignored.",
                placeholder="12345678",
            ),
            TransportField(
                key="TELEGRAM_BRIEF_CHAT_ID",
                label="Brief chat ID (optional)",
                help="Where scheduled briefs / reminders go. Defaults to the first allowed user ID.",
                optional=True,
            ),
            TransportField(
                key="TELEGRAM_ALLOWED_CHAT_IDS",
                label="Allowed group chat IDs (optional)",
                help="Comma-separated. Group chat IDs are negative integers; find via @RawDataBot. Set to opt the bot into specific groups; leave empty for DM-only.",
                optional=True,
            ),
            TransportField(
                key="TELEGRAM_GROUP_TRIGGERS",
                label="Group triggers (optional)",
                help="Extra substrings (the bot's own @-mention is always accepted). Defaults to `@agent, hey agent, agent,`.",
                optional=True,
            ),
        ],
    ),
    TransportDef(
        name="discord",
        label="Discord",
        description=(
            "Bot you create via the Discord Developer Portal. DM + "
            "opt-in server-channel support. Image attachments route "
            "through the vision sub-agent."
        ),
        platform_note="cross-platform",
        setup_url="https://discord.com/developers/applications",
        verify_module="relay.discord_relay",
        fields=[
            TransportField(
                key="DISCORD_BOT_TOKEN",
                label="Bot token",
                help="From Bot tab → Reset Token. Also flip Message Content Intent ON.",
                secret=True,
            ),
            TransportField(
                key="DISCORD_ALLOWED_USER_IDS",
                label="Allowed Discord user IDs",
                help="Comma-separated. Enable Developer Mode, right-click yourself → Copy User ID.",
            ),
            TransportField(
                key="DISCORD_BRIEF_RECIPIENT_ID",
                label="Brief recipient (optional)",
                help="Defaults to first allowed user ID.",
                optional=True,
            ),
            TransportField(
                key="DISCORD_ALLOWED_CHANNEL_IDS",
                label="Allowed channels (optional)",
                help="Comma-separated channel IDs (right-click channel → Copy Channel ID). Opt the bot into server rooms; leave empty for DM-only.",
                optional=True,
            ),
            TransportField(
                key="DISCORD_GROUP_TRIGGERS",
                label="Group triggers (optional)",
                help="The bot's <@id> mention is always accepted. Defaults to `@agent, hey agent, agent,`.",
                optional=True,
            ),
        ],
    ),
    TransportDef(
        name="slack",
        label="Slack",
        description=(
            "Socket Mode app — no public URL needed. DM + opt-in "
            "channel / private-channel / mpim support. Image "
            "attachments via vision."
        ),
        platform_note="cross-platform",
        setup_url="https://api.slack.com/apps",
        verify_module="relay.slack_relay",
        fields=[
            TransportField(
                key="SLACK_BOT_TOKEN",
                label="Bot User OAuth Token",
                help="From OAuth & Permissions → Install to Workspace. Starts `xoxb-`. Required scopes: chat:write, im:history, im:read, files:read, users:read.",
                placeholder="xoxb-...",
                secret=True,
            ),
            TransportField(
                key="SLACK_APP_TOKEN",
                label="App-Level Token",
                help="From Socket Mode → create token with scope `connections:write`. Starts `xapp-`.",
                placeholder="xapp-...",
                secret=True,
            ),
            TransportField(
                key="SLACK_ALLOWED_USER_IDS",
                label="Allowed Slack user IDs",
                help="Comma-separated. Find yours: profile → ⋯ → Copy member ID. Format Uxxxxxxxx.",
                placeholder="U12345678",
            ),
            TransportField(
                key="SLACK_BRIEF_USER_ID",
                label="Brief user (optional)",
                help="Defaults to first allowed user ID.",
                optional=True,
            ),
            TransportField(
                key="SLACK_ALLOWED_CHANNEL_IDS",
                label="Allowed channels (optional)",
                help="Comma-separated. Cxxxxx for public, Gxxxxx for private. Also subscribe message.channels / message.groups / message.mpim in Event Subscriptions.",
                optional=True,
            ),
            TransportField(
                key="SLACK_GROUP_TRIGGERS",
                label="Group triggers (optional)",
                help="The bot's <@user_id> mention is always accepted. Defaults to `@agent, hey agent, agent,`.",
                optional=True,
            ),
        ],
    ),
    TransportDef(
        name="sms",
        label="SMS (via Twilio)",
        description=(
            "Bidirectional SMS via Twilio. Universal reach (any "
            "phone, any carrier) but text-only — no image attachments "
            "and no vision flow on inbound. Costs ~$1/mo for the "
            "number plus ~$0.008 per message. Needs a public URL "
            "for Twilio to deliver webhooks (ngrok for dev, reverse "
            "proxy for prod)."
        ),
        platform_note="cross-platform; needs public URL",
        setup_url="https://www.twilio.com/console",
        verify_module="relay.sms_relay",
        fields=[
            TransportField(
                key="TWILIO_ACCOUNT_SID",
                label="Twilio Account SID",
                help="From Console → Account → API keys & tokens.",
                secret=True,
                placeholder="ACxxxxxxxx...",
            ),
            TransportField(
                key="TWILIO_AUTH_TOKEN",
                label="Twilio Auth Token",
                help="Same panel. Used both for outbound REST calls and to validate inbound webhook signatures.",
                secret=True,
            ),
            TransportField(
                key="TWILIO_FROM_NUMBER",
                label="Your Twilio number",
                help="In E.164 form: +15551234567. Has to be an SMS-capable number from Phone Numbers → Manage → Active numbers.",
                placeholder="+15551234567",
                secret=True,
            ),
            TransportField(
                key="SMS_ALLOWED_NUMBERS",
                label="Allowed sender numbers",
                help="Comma-separated, E.164. The relay drops every other inbound message. Set to at least your own phone.",
                placeholder="+15551234567",
                secret=True,
            ),
            TransportField(
                key="SMS_WEBHOOK_PORT",
                label="Webhook port",
                help="Local port the FastAPI webhook listens on. Bound to 127.0.0.1 — expose via ngrok or reverse proxy.",
                default="8781",
                optional=True,
            ),
            TransportField(
                key="SMS_BRIEF_RECIPIENT",
                label="Brief recipient (optional)",
                help="Defaults to the first number in SMS_ALLOWED_NUMBERS.",
                optional=True,
            ),
        ],
    ),
]


_TRANSPORT_BY_NAME = {t.name: t for t in TRANSPORTS}


# ─── Live env helpers (own minimal versions to avoid coupling to /config/env) ─


def _read_env_dict() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    parsed = dotenv_values(ENV_PATH) or {}
    return {k: (v or "") for k, v in parsed.items()}


def _current_transport(env: dict[str, str]) -> str:
    return (env.get("RELAY_TRANSPORT") or "imessage").strip().lower() or "imessage"


# At most one verify subprocess at a time to keep output coherent.
_active_verify: dict[str, asyncio.subprocess.Process] = {}


# ─── Routes ────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    env = _read_env_dict()
    active = _current_transport(env)
    selected_name = (request.query_params.get("pick") or active).strip().lower()
    if selected_name not in _TRANSPORT_BY_NAME:
        selected_name = active if active in _TRANSPORT_BY_NAME else "imessage"
    selected = _TRANSPORT_BY_NAME[selected_name]

    # Hydrate selected transport's field values from .env.
    field_values: list[dict] = []
    for f in selected.fields:
        current_value = env.get(f.key, "")
        field_values.append({
            "key": f.key,
            "label": f.label,
            "help": f.help,
            "secret": f.secret,
            "optional": f.optional,
            "placeholder": f.placeholder,
            "default": f.default,
            "select_options": f.select_options,
            "value": current_value,
            "is_set": bool(current_value.strip()),
        })

    return templates.TemplateResponse(
        request, "settings/transports.html",
        {
            "transports": TRANSPORTS,
            "selected": selected,
            "selected_fields": field_values,
            "active_transport": active,
            "saved": request.query_params.get("saved") == "1",
            "verify_running": selected.name in _active_verify
                              and _active_verify[selected.name].returncode is None,
        },
    )


@router.post("/save")
async def save(request: Request) -> RedirectResponse:
    """Write RELAY_TRANSPORT + the selected transport's fields to .env.

    Preserves comments and blank lines in the existing .env. Empty
    field values are written as `KEY=` (matches the rest of the
    install flow) rather than removed, so the editor at /config/env
    keeps surfacing them.
    """
    form = await request.form()
    transport_name = (form.get("transport") or "").strip().lower()
    if transport_name not in _TRANSPORT_BY_NAME:
        raise HTTPException(400, f"unknown transport: {transport_name!r}")
    transport = _TRANSPORT_BY_NAME[transport_name]

    # Collect the new key/value map from the form.
    new_values: dict[str, str] = {"RELAY_TRANSPORT": transport_name}
    for f in transport.fields:
        raw = form.get(f"field:{f.key}")
        if raw is None:
            continue
        new_values[f.key] = str(raw).strip()

    # Rewrite .env, replacing values in-place where keys already
    # exist, appending the rest at the bottom under a small header.
    if not ENV_PATH.exists():
        raise HTTPException(404, ".env not found — run install.sh first")

    existing_lines = ENV_PATH.read_text().splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = line.partition("=")[0].strip()
            if key in new_values:
                out.append(f"{key}={new_values[key]}")
                seen.add(key)
                continue
        out.append(line)

    appended: list[str] = []
    for key, value in new_values.items():
        if key in seen:
            continue
        appended.append(f"{key}={value}")
    if appended:
        if out and out[-1].strip():
            out.append("")
        out.append("# ── Added from /settings/transports ──")
        out.extend(appended)

    ENV_PATH.write_text("\n".join(out) + "\n")
    os.chmod(ENV_PATH, 0o600)
    return RedirectResponse(f"?pick={transport_name}&saved=1", status_code=303)


# ─── Verify (subprocess + SSE) ─────────────────────────────────────────────


@router.post("/verify/{name}")
async def start_verify(name: str) -> JSONResponse:
    """Spawn `python -m <transport.verify_module> --check`. Returns
    the SSE stream URL."""
    transport = _TRANSPORT_BY_NAME.get(name)
    if not transport:
        raise HTTPException(404, f"unknown transport: {name!r}")
    if name in _active_verify and _active_verify[name].returncode is None:
        return JSONResponse(
            {"ok": True, "stream": f"/settings/transports/verify/{name}/stream",
             "note": "already running — re-attaching"}
        )

    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", transport.verify_module, "--check",
        cwd=str(V1_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ},
    )
    _active_verify[name] = proc
    return JSONResponse({"ok": True, "stream": f"/settings/transports/verify/{name}/stream"})


@router.get("/verify/{name}/stream")
async def stream_verify(name: str):
    proc = _active_verify.get(name)
    if proc is None:
        raise HTTPException(404, f"no active verify run for {name!r}")

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
            if proc.returncode is not None:
                _active_verify.pop(name, None)

    return EventSourceResponse(events())
