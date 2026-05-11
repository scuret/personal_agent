"""Dropbox OAuth refresh-token flow.

Replaces the short-lived `sl.u.` access token (4-hour expiry) with the
standard OAuth code-flow that returns BOTH an access token and a
refresh token. The refresh token is long-lived and is used to mint
fresh access tokens automatically when the cached one expires.

Env vars (set in .env):

  DROPBOX_APP_KEY     The app's client ID (public-ish; from the app's
                      Settings page at dropbox.com/developers/apps).

  DROPBOX_APP_SECRET  The app's client secret. Treat like a password.
                      Same Settings page → "App secret → Show".

  DROPBOX_REDIRECT_PORT  Optional. Local port the consent flow listens
                         on. Default 53682 (matches Dropbox's official
                         Python SDK). MUST match the Redirect URI you
                         pre-registered in the app's Settings.

Token cache:

  DROPBOX_TOKEN_PATH  Default: data/dropbox_token.json. Stores
                      {access_token, refresh_token, expires_at}.

App setup at dropbox.com/developers/apps (do this BEFORE the CLI flow):

  1. Open your app → Settings tab.
  2. In "Redirect URIs", add EXACTLY: http://localhost:53682
     (no trailing slash; lowercase 'localhost'; port 53682). Click Add.
  3. Save changes if there's a Save button.
  4. From the same Settings page, copy App key + App secret into .env.

First-time consent at the Mac:

  python -m mcp_servers.dropbox_auth

That pops a browser to dropbox.com's consent page; click Allow. The
local helper catches the redirect, exchanges the code for tokens, and
writes the cache. From then on the server side just calls
`get_access_token()`.
"""

from __future__ import annotations

import json
import os
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

AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropbox.com/oauth2/token"
DEFAULT_TOKEN_PATH = "./data/dropbox_token.json"

# Default local port for the consent redirect. Matches the port the
# official Dropbox Python SDK uses for its installed-app auth flow, so
# users who've ever configured a Dropbox local-OAuth app may already
# have this whitelisted. Override with DROPBOX_REDIRECT_PORT.
DEFAULT_REDIRECT_PORT = 53682

# Refresh a token if it expires within this many seconds — gives a small
# buffer so an in-flight request doesn't race the expiry.
_REFRESH_LEAD_SECONDS = 120


def _v1_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _token_path() -> Path:
    raw = os.environ.get("DROPBOX_TOKEN_PATH", DEFAULT_TOKEN_PATH)
    return Path(raw) if Path(raw).is_absolute() else (_v1_dir() / raw)


def _app_creds() -> tuple[str, str]:
    key = (os.environ.get("DROPBOX_APP_KEY") or "").strip()
    secret = (os.environ.get("DROPBOX_APP_SECRET") or "").strip()
    if not key or not secret:
        raise RuntimeError(
            "DROPBOX_APP_KEY and DROPBOX_APP_SECRET must be set. Create or "
            "open your app at dropbox.com/developers/apps → Settings, then "
            "copy the App key and the App secret into .env."
        )
    return key, secret


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


def _exchange_code_for_tokens(code: str, redirect_uri: str) -> dict[str, Any]:
    key, secret = _app_creds()
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "grant_type": "authorization_code",
            "client_id": key,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    key, secret = _app_creds()
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": key,
            "client_secret": secret,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


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


def get_access_token() -> str:
    """Return a valid Dropbox access token, refreshing if necessary.

    Raises if there's no cached refresh token yet (i.e. first-time
    consent hasn't been done). The error message points the user at the
    CLI entrypoint.
    """
    cache = _load_cache()
    if not cache or not cache.get("refresh_token"):
        raise RuntimeError(
            "no cached Dropbox token. run "
            "`python -m mcp_servers.dropbox_auth` at the Mac to grant "
            "access and seed the refresh token."
        )

    if cache.get("access_token") and not _is_expired(cache.get("expires_at")):
        return cache["access_token"]

    # Refresh.
    fresh = _refresh_access_token(cache["refresh_token"])
    cache["access_token"] = fresh["access_token"]
    cache["expires_at"] = _expires_at_from(fresh)
    # Dropbox usually re-issues the same refresh token, but in case the
    # response carries a new one (token rotation), persist it.
    if "refresh_token" in fresh:
        cache["refresh_token"] = fresh["refresh_token"]
    _save_cache(cache)
    return cache["access_token"]


# ─── First-time CLI consent flow ───────────────────────────────────────────


class _CodeCatcher(BaseHTTPRequestHandler):
    """One-shot HTTP server that catches Dropbox's redirect with ?code=."""

    captured_code: str | None = None
    captured_error: str | None = None

    def do_GET(self):  # noqa: N802 — required by stdlib BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _CodeCatcher.captured_code = params["code"][0]
            body = b"<html><body><h2>auth ok.</h2><p>you can close this window.</p></body></html>"
        elif "error" in params:
            _CodeCatcher.captured_error = params.get("error_description", params["error"])[0]
            body = b"<html><body><h2>auth failed.</h2></body></html>"
        else:
            body = b"<html><body>unexpected redirect.</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        # Silence the default request logging.
        pass


def _redirect_port() -> int:
    """Return the fixed port the consent server should listen on.

    Dropbox requires the redirect_uri to match a pre-registered entry
    in the app's Settings exactly (including path and port), so we
    can't pick a free port at runtime. Default is 53682; override
    with DROPBOX_REDIRECT_PORT if that's taken or you'd rather use a
    different value (just make sure the app's Redirect URIs list has
    the matching entry).
    """
    raw = os.environ.get("DROPBOX_REDIRECT_PORT")
    if raw and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            pass
    return DEFAULT_REDIRECT_PORT


def run_interactive_auth() -> None:
    """Open a browser for the user, catch the redirect, persist tokens.

    This is the first-time setup path. After it succeeds, the cache
    file holds a refresh token and get_access_token() handles every
    subsequent token request silently.
    """
    key, _secret = _app_creds()
    port = _redirect_port()
    redirect_uri = f"http://localhost:{port}"
    params = {
        "client_id": key,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        # offline = give us a refresh_token along with the access_token.
        "token_access_type": "offline",
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    print(f"redirect URI for this run: {redirect_uri}")
    print(
        "NOTE: Dropbox requires this exact URI to appear in your app's "
        "Settings → Redirect URIs list. If you get 'Invalid redirect_uri', "
        "add it at dropbox.com/developers/apps, save, and re-run."
    )
    print()
    print("opening browser for Dropbox consent:")
    print(f"  {auth_url}")
    print()
    print(f"listening for redirect on port {port}…")

    server = HTTPServer(("127.0.0.1", port), _CodeCatcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    webbrowser.open(auth_url)

    # Wait up to 5 minutes for the user to click Allow.
    deadline = time.time() + 300
    while time.time() < deadline:
        if _CodeCatcher.captured_code or _CodeCatcher.captured_error:
            break
        time.sleep(0.5)
    server.shutdown()

    if _CodeCatcher.captured_error:
        raise RuntimeError(f"Dropbox auth failed: {_CodeCatcher.captured_error}")
    if not _CodeCatcher.captured_code:
        raise RuntimeError("Dropbox auth timed out before the redirect arrived.")

    tokens = _exchange_code_for_tokens(_CodeCatcher.captured_code, redirect_uri)
    if "refresh_token" not in tokens:
        raise RuntimeError(
            "Dropbox returned no refresh_token. Verify the app is set up "
            "with offline access (token_access_type=offline) and the user "
            "granted consent."
        )
    cache = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": _expires_at_from(tokens),
        "account_id": tokens.get("account_id"),
    }
    _save_cache(cache)


def main() -> None:
    """CLI entrypoint for first-time consent."""
    from dotenv import load_dotenv  # late import — keep auth helper light

    load_dotenv()
    try:
        _app_creds()  # validates env presence before browser pop
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"token cache: {_token_path()}")
    existing = _load_cache()
    if existing and existing.get("refresh_token"):
        print("an existing refresh token is cached. re-running will overwrite it.")
    print()
    run_interactive_auth()
    print()
    print(f"ok. tokens saved to {_token_path()}.")
    cache = _load_cache() or {}
    print(f"account_id: {cache.get('account_id', '?')}")
    print(f"access token expires at: {cache.get('expires_at', '?')}")
    print("future agent runs will auto-refresh; no further action needed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
