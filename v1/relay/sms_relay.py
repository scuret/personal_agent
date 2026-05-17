"""SMS relay — fifth transport option, via Twilio.

Universal reach (any phone, any carrier, no app to install). Text-only
— no image attachments and therefore no vision flow on inbound (a
caveat to flag; iMessage / Telegram / Discord / Slack all keep vision
working).

Architecture (different shape from the other relays):

  * Twilio is webhook-based. Twilio POSTs each inbound SMS to a URL
    you configure in their console. This relay hosts a tiny FastAPI
    app at `POST /sms/webhook` to receive them.
  * That URL has to be publicly reachable. For local dev that means
    running ngrok (or any HTTPS tunnel) in a separate terminal:
        ngrok http 8781
    and pasting the resulting `https://....ngrok-free.app/sms/webhook`
    into the Twilio number's "A MESSAGE COMES IN" webhook field.
  * For a hosted deploy (always-on home server, VPS) you put the
    relay behind a reverse proxy with HTTPS termination instead.
  * Replies go OUT through Twilio's REST API — they don't echo back
    through the same webhook, so there's no loop-prevention marker
    to track (unlike iMessage).

Setup:
  1. Sign up at twilio.com. Buy an SMS-capable phone number (~$1/mo).
  2. Console → Account → API keys → grab Account SID + Auth Token.
     Save as TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env.
  3. Phone Numbers → Manage → Active numbers → click your number.
     Save the E.164 form (`+15551234567`) as TWILIO_FROM_NUMBER.
  4. Set SMS_ALLOWED_NUMBERS in .env to a comma-separated list of
     numbers (also E.164). The relay drops messages from anyone not
     on this list.
  5. Set SMS_WEBHOOK_PORT (default 8781) in .env if you want a
     non-default port.
  6. Start the relay (`launchctl kickstart -k
     gui/$(id -u)/com.personal-agent.relay`) and confirm it logs
     "relay started, listening on http://127.0.0.1:8781/sms/webhook".
  7. In a second terminal: `ngrok http 8781`. Copy the HTTPS URL.
  8. Twilio console → your number → Messaging configuration → set
     "A MESSAGE COMES IN" to `<ngrok-url>/sms/webhook` (POST). Save.
  9. Text your number from your phone — you should get a reply.

Cost: ~$1/mo for the number + ~$0.0079/msg inbound + ~$0.0079/msg
outbound. A typical day at agent-tier volumes is sub-dollar.

Security: Twilio signs every webhook with `X-Twilio-Signature`. The
relay validates that against TWILIO_AUTH_TOKEN before processing.
Forged inbound messages won't go through.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Late SDK imports so .env is in place first.
from claude_agent_sdk import ClaudeSDKClient  # noqa: E402

from agent_host import build_options, process_turn  # noqa: E402
from memory.store import MemoryStore  # noqa: E402

CONVERSATION_SOURCE = "sms"
CONVERSATION_GAP_HOURS = 4.0
DEFAULT_PORT = 8781

# Maximum SMS body length per Twilio: 1600 chars (multi-segment).
# Agent replies that exceed this get split at line boundaries.
_SMS_MAX_CHARS = 1500


# ─── Config ────────────────────────────────────────────────────────────────


def _account_sid() -> str:
    v = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    if not v:
        raise RuntimeError("TWILIO_ACCOUNT_SID not set in .env")
    return v


def _auth_token() -> str:
    v = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not v:
        raise RuntimeError("TWILIO_AUTH_TOKEN not set in .env")
    return v


def _from_number() -> str:
    v = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if not v:
        raise RuntimeError(
            "TWILIO_FROM_NUMBER not set in .env "
            "(your Twilio number in E.164 form, e.g. +15551234567)"
        )
    return v


def _allowed_numbers() -> set[str]:
    raw = os.environ.get("SMS_ALLOWED_NUMBERS", "").strip()
    if not raw:
        return set()
    out: set[str] = set()
    for chunk in raw.split(","):
        c = chunk.strip()
        if c:
            out.add(c)
    return out


def _webhook_port() -> int:
    try:
        return int(os.environ.get("SMS_WEBHOOK_PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


def _resolve_sms_recipient() -> str:
    """Default recipient for scheduler-driven sends (briefs / reminders).
    First number in SMS_ALLOWED_NUMBERS, or SMS_BRIEF_RECIPIENT override.
    """
    override = os.environ.get("SMS_BRIEF_RECIPIENT", "").strip()
    if override:
        return override
    allowed = sorted(_allowed_numbers())
    if not allowed:
        raise RuntimeError(
            "SMS_ALLOWED_NUMBERS not set — needed to know who to text "
            "for scheduled messages"
        )
    return allowed[0]


# ─── Sender (used by relay/sender.py and the inbound webhook handler) ──────


class SMSSender:
    """Outbound SMS via Twilio's REST API. One Twilio client per
    sender instance; cheap to construct."""

    def __init__(self, recipient_number: str) -> None:
        self.recipient = recipient_number
        # Late import — `twilio` is a non-trivial dep and we don't
        # want to pay it for non-SMS transports.
        from twilio.rest import Client  # noqa: PLC0415

        self.client = Client(_account_sid(), _auth_token())
        self.from_number = _from_number()

    def send(self, text: str) -> tuple[bool, str]:
        try:
            for chunk in _split_for_sms(text):
                self.client.messages.create(
                    to=self.recipient,
                    from_=self.from_number,
                    body=chunk,
                )
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


def _split_for_sms(text: str, limit: int = _SMS_MAX_CHARS) -> list[str]:
    """Split a long reply at line boundaries so each SMS stays under
    Twilio's segment-concatenation ceiling. Briefs almost always fit
    in one message; this is defensive for unusually long replies.

    When a single line itself exceeds the limit, we fall back to a
    character-level slice so Twilio doesn't reject the API call.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    used = 0
    for line in text.splitlines(keepends=True):
        if used + len(line) > limit and buf:
            chunks.append("".join(buf))
            buf = []
            used = 0
        # If a single line is itself too long, hard-slice it.
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        buf.append(line)
        used += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


# ─── Webhook + daemon ──────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_app(
    store: MemoryStore,
    sdk_client: ClaudeSDKClient,
    allowed: set[str],
) -> Any:
    """Construct the FastAPI app. Done inside _run_daemon so the SDK
    client + store are captured in the request handler's closure
    without globals."""
    from fastapi import FastAPI, Form, Header, HTTPException, Request  # noqa: PLC0415
    from fastapi.responses import PlainTextResponse  # noqa: PLC0415
    from twilio.request_validator import RequestValidator  # noqa: PLC0415

    app = FastAPI(
        title="personal_agent SMS webhook",
        docs_url=None,
        redoc_url=None,
    )

    auth_token = _auth_token()
    validator = RequestValidator(auth_token)

    @app.post("/sms/webhook", response_class=PlainTextResponse)
    async def sms_webhook(
        request: Request,
        From: str = Form(""),  # noqa: N803 — Twilio's form fields use TitleCase
        Body: str = Form(""),  # noqa: N803
        x_twilio_signature: str = Header(default=""),
    ) -> str:
        # Validate Twilio's HMAC over the URL + form params. Reject
        # anything that doesn't match — guards against forged inbound
        # POSTs to the public webhook URL.
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if not validator.validate(str(request.url), params, x_twilio_signature):
            print(
                f"[sms] rejecting webhook with bad signature (from={From!r})",
                file=sys.stderr,
            )
            raise HTTPException(status_code=403, detail="invalid signature")

        sender = (From or "").strip()
        body = (Body or "").strip()
        if sender not in allowed:
            print(f"[sms] ignoring message from unallowed number {sender!r}")
            return ""  # Twilio expects 2xx
        if not body:
            return ""

        conversation_id = store.resume_or_open_conversation(
            source=CONVERSATION_SOURCE,
            gap_threshold_hours=CONVERSATION_GAP_HOURS,
            metadata={"from": sender},
        )

        print(f"[in @ {_now_iso()}] from={sender}: {body[:20]}")
        try:
            reply = await process_turn(sdk_client, store, conversation_id, body)
        except Exception as e:  # noqa: BLE001
            print(f"[sms] agent error: {e}", file=sys.stderr)
            return ""

        if not reply:
            print("[sms] no reply from agent")
            return ""

        # Send the reply via Twilio REST. We don't TwiML-respond
        # because TwiML's single-message ceiling is lower and we'd
        # have to handle multi-segment splitting differently. REST
        # also gives us delivery receipts in Twilio's dashboard.
        try:
            outbound = SMSSender(sender)
            ok, err = outbound.send(reply)
            if ok:
                print(f"[out → {sender}] {reply[:20]}")
            else:
                print(f"[sms send failed] {err}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[sms send error] {e}", file=sys.stderr)
        return ""

    @app.get("/sms/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    return app


async def _run_daemon() -> None:
    import uvicorn  # noqa: PLC0415

    # Auto-restart on .env change so chat-driven sub-agent toggles +
    # web-UI key saves take effect within ~10s without a manual kick.
    from tools.env_watcher import watch_env_and_exit_on_change
    asyncio.create_task(
        watch_env_and_exit_on_change(log_prefix="[env-watch sms]")
    )

    store = MemoryStore()
    allowed = _allowed_numbers()
    if not allowed:
        print(
            "WARNING: SMS_ALLOWED_NUMBERS is empty — relay will reject "
            "every inbound message. Add at least your own phone number "
            "in E.164 form (e.g. +15551234567).",
            file=sys.stderr,
        )

    # Eagerly validate the Twilio config so we fail loudly at startup,
    # not on the first webhook hit.
    _account_sid()
    _auth_token()
    _from_number()

    options = build_options(store)
    sdk_client = ClaudeSDKClient(options=options)
    await sdk_client.__aenter__()

    app = _build_app(store, sdk_client, allowed)
    port = _webhook_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    print(
        f"[sms] relay started — listening on http://127.0.0.1:{port}/sms/webhook "
        f"(allowed: {sorted(allowed)})"
    )
    print(
        "[sms] expose this URL publicly via ngrok or a reverse proxy and "
        "paste it into Twilio's 'A MESSAGE COMES IN' webhook field."
    )

    try:
        await server.serve()
    finally:
        try:
            await sdk_client.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


# ─── Diagnostics ───────────────────────────────────────────────────────────


def _check() -> int:
    print("=== sms relay diagnostics ===\n")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY not set")
        return 1
    print("✓ ANTHROPIC_API_KEY set")

    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        v = os.environ.get(var, "").strip()
        if not v:
            print(f"✗ {var} not set")
            return 1
        shown = v[:6] + "…" if len(v) > 8 else v
        print(f"✓ {var} = {shown}")

    allowed = _allowed_numbers()
    if not allowed:
        print("✗ SMS_ALLOWED_NUMBERS empty — relay will reject every inbound message")
        return 1
    print(f"✓ SMS_ALLOWED_NUMBERS = {sorted(allowed)}")
    print(f"  webhook port = {_webhook_port()} (bound to 127.0.0.1)")

    try:
        from twilio.rest import Client  # noqa: PLC0415

        client = Client(_account_sid(), _auth_token())
        # Cheap sanity check: fetch the account record. This is one
        # API call and confirms the credentials are valid.
        acct = client.api.accounts(_account_sid()).fetch()
        print(f"✓ Twilio account verified: {acct.friendly_name} (status={acct.status})")
    except Exception as e:  # noqa: BLE001
        print(f"✗ Twilio API check failed: {e}")
        return 1

    print()
    print("All green. Next:")
    print("  1. Start the relay (or kickstart the LaunchAgent).")
    print("  2. In a second terminal: ngrok http", _webhook_port())
    print("  3. Paste the ngrok HTTPS URL + '/sms/webhook' into Twilio's")
    print("     'A MESSAGE COMES IN' webhook field for your number.")
    print("  4. Text your Twilio number from an allowed phone.")
    return 0


# ─── Entry point ───────────────────────────────────────────────────────────


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(_check())
    try:
        asyncio.run(_run_daemon())
    except KeyboardInterrupt:
        print("\n[sms] relay stopped.")


if __name__ == "__main__":
    main()
