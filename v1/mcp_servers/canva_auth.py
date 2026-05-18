"""Canva OAuth refresh-token flow (Canva Connect API).

Standard authorization-code flow with refresh tokens. Access tokens last
~4 hours; this module refreshes them automatically in-process when one
is close to expiry.

Env vars (set in .env):

  CANVA_CLIENT_ID       App's client ID, from developer.canva.com.
  CANVA_CLIENT_SECRET   App's client secret. Treat like a password.
  CANVA_REDIRECT_PORT   Optional. Local port for consent redirect.
                        Default 8767 — must match the Redirect URI you
                        register in the Canva developer console.

Token cache:

  CANVA_TOKEN_PATH      Default: data/canva_token.json. Stores
                        {access_token, refresh_token, expires_at, scope}.

App setup at developer.canva.com (once, before the CLI flow):

  1. Sign in → Your integrations → Create an integration.
  2. Integration type: "Public" or "Private" (Private is fine for a
     personal agent — no review required).
  3. Configuration → Authentication → add Redirect URL:
       http://127.0.0.1:8767
     (exact match; literal 127.0.0.1).
  4. Configuration → Scopes → enable at minimum:
       design:meta:read, design:content:read, folder:read, profile:read
     For create/export tools, also enable design:content:write and
     asset:read.
  5. Copy Client ID and Client Secret → .env.

First-time CLI consent at the Mac:

  python -m mcp_servers.canva_auth

Browser opens to canva.com/oauth — click Allow. The local helper
catches the redirect, exchanges code → tokens, writes the cache.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests

AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"
DEFAULT_TOKEN_PATH = "./data/canva_token.json"
DEFAULT_REDIRECT_PORT = 8767

_REFRESH_LEAD_SECONDS = 120

# Scopes the sub-agent's tools require. PKCE-friendly subset; expand
# only when a new tool needs an additional scope (re-auth picks up new
# scopes, old cache gets discarded on mismatch).
SCOPES: list[str] = [
    "design:meta:read",
    "design:content:read",
    "design:content:write",
    "folder:read",
    "asset:read",
    "profile:read",
]


def _v1_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _token_path() -> Path:
    from core.paths import oauth_token_path
    return oauth_token_path("canva", env_var="CANVA_TOKEN_PATH")


def _redirect_port() -> int:
    raw = os.environ.get("CANVA_REDIRECT_PORT")
    if raw and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            pass
    return DEFAULT_REDIRECT_PORT


def _app_creds() -> tuple[str, str]:
    cid = (os.environ.get("CANVA_CLIENT_ID") or "").strip()
    sec = (os.environ.get("CANVA_CLIENT_SECRET") or "").strip()
    if not cid or not sec:
        raise RuntimeError(
            "CANVA_CLIENT_ID and CANVA_CLIENT_SECRET must be set. Create "
            "an integration at developer.canva.com, then copy the Client "
            "ID and Client Secret into .env."
        )
    return cid, sec


def _basic_auth_header() -> dict[str, str]:
    cid, sec = _app_creds()
    encoded = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _load_cache() -> dict[str, Any] | None:
    path = _token_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(data: dict[str, Any]) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    # Token cache holds live refresh tokens — owner-only. ROADMAP H1.
    os.chmod(path, 0o600)


def _expires_at_from(payload: dict[str, Any]) -> str:
    seconds = int(payload.get("expires_in", 14400))
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        dt = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    return (dt - datetime.now(timezone.utc)).total_seconds() < _REFRESH_LEAD_SECONDS


def _scopes_satisfied(cached: dict[str, Any]) -> bool:
    have = set((cached.get("scope") or "").split())
    return all(s in have for s in SCOPES)


def _pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _exchange_code_for_tokens(code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
    resp = requests.post(
        TOKEN_URL,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    resp = requests.post(
        TOKEN_URL,
        headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def get_access_token() -> str:
    """Return a valid Canva access token, refreshing if necessary."""
    cache = _load_cache()
    if not cache or not cache.get("refresh_token"):
        raise RuntimeError(
            "no cached Canva token. run "
            "`python -m mcp_servers.canva_auth` at the Mac to grant "
            "access and seed the refresh token."
        )
    if not _scopes_satisfied(cache):
        raise RuntimeError(
            "cached Canva token is missing one or more required scopes. "
            "re-run `python -m mcp_servers.canva_auth` to consent."
        )
    if cache.get("access_token") and not _is_expired(cache.get("expires_at")):
        return cache["access_token"]

    fresh = _refresh_access_token(cache["refresh_token"])
    cache["access_token"] = fresh["access_token"]
    cache["expires_at"] = _expires_at_from(fresh)
    if "refresh_token" in fresh:
        cache["refresh_token"] = fresh["refresh_token"]
    if "scope" in fresh:
        cache["scope"] = fresh["scope"]
    _save_cache(cache)
    return cache["access_token"]


# ─── First-time CLI consent flow ───────────────────────────────────────────


class _CodeCatcher(BaseHTTPRequestHandler):
    """Catches the Canva OAuth redirect.

    Security batch 5 (C1): validates `state` against `expected_state`
    (was already generated and sent in the auth URL, but the previous
    implementation never checked it on the callback). Mismatch = CSRF
    attempt, treated as an auth error.
    """

    captured_code: str | None = None
    captured_error: str | None = None
    expected_state: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        returned_state = (params.get("state") or [""])[0]
        if (
            _CodeCatcher.expected_state
            and returned_state != _CodeCatcher.expected_state
        ):
            _CodeCatcher.captured_error = (
                "CSRF: OAuth state mismatch "
                f"(expected {_CodeCatcher.expected_state[:8]}…, got {returned_state[:8]!r})"
            )
            body = b"<html><body><h2>auth failed.</h2><p>state mismatch (CSRF check).</p></body></html>"
        elif "code" in params:
            _CodeCatcher.captured_code = params["code"][0]
            body = b"<html><body><h2>auth ok.</h2><p>you can close this window.</p></body></html>"
        elif "error" in params:
            _CodeCatcher.captured_error = params.get(
                "error_description", params["error"]
            )[0]
            body = b"<html><body><h2>auth failed.</h2></body></html>"
        else:
            body = b"<html><body>unexpected redirect.</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


def run_interactive_auth() -> None:
    """Open browser, catch redirect, persist tokens.

    Canva requires the redirect URI to be whitelisted exactly in the
    integration's configuration. Default is http://127.0.0.1:8767;
    override with CANVA_REDIRECT_PORT (and add the matching URL to the
    integration's Redirect URLs list).
    """
    cid, _ = _app_creds()
    port = _redirect_port()
    redirect_uri = f"http://127.0.0.1:{port}"
    code_verifier, code_challenge = _pkce_pair()
    # Prime the catcher to enforce the state we send. Was previously
    # only generated and sent, never validated — opened a narrow CSRF
    # window in the 5-min callback period.
    state = secrets.token_urlsafe(32)
    _CodeCatcher.expected_state = state
    _CodeCatcher.captured_code = None
    _CodeCatcher.captured_error = None
    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    print(f"redirect URI for this run: {redirect_uri}")
    print(
        "NOTE: Canva requires this exact URI to be in your integration's "
        "Redirect URLs list at developer.canva.com. If you get a "
        "redirect_uri error, add it and re-run."
    )
    print()
    print("opening browser for Canva consent:")
    print(f"  {auth_url}")
    print()
    print(f"listening for redirect on port {port}…")

    server = HTTPServer(("127.0.0.1", port), _CodeCatcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    webbrowser.open(auth_url)

    deadline = time.time() + 300
    while time.time() < deadline:
        if _CodeCatcher.captured_code or _CodeCatcher.captured_error:
            break
        time.sleep(0.5)
    server.shutdown()

    if _CodeCatcher.captured_error:
        raise RuntimeError(f"Canva auth failed: {_CodeCatcher.captured_error}")
    if not _CodeCatcher.captured_code:
        raise RuntimeError("Canva auth timed out before the redirect arrived.")

    tokens = _exchange_code_for_tokens(
        _CodeCatcher.captured_code, redirect_uri, code_verifier
    )
    if "refresh_token" not in tokens:
        raise RuntimeError("Canva returned no refresh_token — unexpected.")
    cache = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": _expires_at_from(tokens),
        "scope": tokens.get("scope", " ".join(SCOPES)),
    }
    _save_cache(cache)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    try:
        _app_creds()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"token cache: {_token_path()}")
    print(f"requesting scopes: {SCOPES}")
    existing = _load_cache()
    if existing and existing.get("refresh_token"):
        print("an existing refresh token is cached. re-running will overwrite it.")
    print()
    run_interactive_auth()
    print()
    print(f"ok. tokens saved to {_token_path()}.")
    cache = _load_cache() or {}
    print(f"granted scope: {cache.get('scope', '?')}")
    print(f"access token expires at: {cache.get('expires_at', '?')}")
    print("future agent runs will auto-refresh; no further action needed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
