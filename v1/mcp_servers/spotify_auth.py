"""Spotify OAuth refresh-token flow.

Standard Spotify Web API auth: code grant with refresh tokens. Access
tokens expire after 1 hour; this module refreshes automatically in
process when one's about to expire.

Env vars (set in .env):

  SPOTIFY_CLIENT_ID       App's client ID, from developer.spotify.com/dashboard.
  SPOTIFY_CLIENT_SECRET   App's client secret. Treat like a password.

Token cache:

  SPOTIFY_TOKEN_PATH      Default: data/spotify_token.json. Stores
                          {access_token, refresh_token, expires_at, scope}.

App setup (do this once on the web before the CLI flow):

  1. https://developer.spotify.com/dashboard → Create app.
  2. Redirect URIs — add `http://127.0.0.1:8765` (any free local port
     works; this module asks Spotify for whatever port the local server
     binds to. 127.0.0.1 is required by Spotify in 2025 — `localhost`
     is rejected).
  3. Save. Note the Client ID and Client Secret on the app's Settings
     page; paste into .env.

First-time CLI consent at the Mac:

  python -m mcp_servers.spotify_auth

Browser pops to Spotify's consent page; click Agree. The local helper
catches the redirect, exchanges code → tokens, writes the cache.
"""

from __future__ import annotations

import base64
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

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
DEFAULT_TOKEN_PATH = "./data/spotify_token.json"

# Spotify access tokens last ~3600s. Refresh with a 2-minute buffer.
_REFRESH_LEAD_SECONDS = 120

# Scope set the sub-agent needs. Keep tight — add scopes only when a
# tool actually requires it (re-auth picks up new scopes; old token
# cache gets discarded on mismatch).
SCOPES: list[str] = [
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-library-read",
    "user-library-modify",
    "playlist-read-private",
    "playlist-modify-private",
    "playlist-modify-public",
]


def _v1_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _token_path() -> Path:
    raw = os.environ.get("SPOTIFY_TOKEN_PATH", DEFAULT_TOKEN_PATH)
    return Path(raw) if Path(raw).is_absolute() else (_v1_dir() / raw)


def _app_creds() -> tuple[str, str]:
    cid = (os.environ.get("SPOTIFY_CLIENT_ID") or "").strip()
    sec = (os.environ.get("SPOTIFY_CLIENT_SECRET") or "").strip()
    if not cid or not sec:
        raise RuntimeError(
            "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set. "
            "Create or open your app at developer.spotify.com/dashboard, "
            "then copy Client ID and Client Secret into .env."
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


def _expires_at_from(payload: dict[str, Any]) -> str:
    seconds = int(payload.get("expires_in", 3600))
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


def _exchange_code_for_tokens(code: str, redirect_uri: str) -> dict[str, Any]:
    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth_header(),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    resp = requests.post(
        TOKEN_URL,
        headers=_basic_auth_header(),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def get_access_token() -> str:
    """Return a valid Spotify access token, refreshing if necessary."""
    cache = _load_cache()
    if not cache or not cache.get("refresh_token"):
        raise RuntimeError(
            "no cached Spotify token. run "
            "`python -m mcp_servers.spotify_auth` at the Mac to grant "
            "access and seed the refresh token."
        )

    if not _scopes_satisfied(cache):
        raise RuntimeError(
            "cached Spotify token is missing one or more required scopes. "
            "re-run `python -m mcp_servers.spotify_auth` to consent to "
            "the updated scope set."
        )

    if cache.get("access_token") and not _is_expired(cache.get("expires_at")):
        return cache["access_token"]

    fresh = _refresh_access_token(cache["refresh_token"])
    cache["access_token"] = fresh["access_token"]
    cache["expires_at"] = _expires_at_from(fresh)
    # Spotify usually does NOT rotate the refresh token, but if a new
    # one is returned, persist it.
    if "refresh_token" in fresh:
        cache["refresh_token"] = fresh["refresh_token"]
    if "scope" in fresh:
        cache["scope"] = fresh["scope"]
    _save_cache(cache)
    return cache["access_token"]


# ─── First-time CLI consent flow ───────────────────────────────────────────


class _CodeCatcher(BaseHTTPRequestHandler):
    captured_code: str | None = None
    captured_error: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _CodeCatcher.captured_code = params["code"][0]
            body = b"<html><body><h2>auth ok.</h2><p>you can close this window.</p></body></html>"
        elif "error" in params:
            _CodeCatcher.captured_error = params["error"][0]
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_interactive_auth(port: int | None = None) -> None:
    """Open browser, catch redirect, persist tokens.

    Spotify requires the redirect URI to be whitelisted exactly in the
    app dashboard. The default whitelisted entry the user is told to
    add is `http://127.0.0.1:8765`; if `port` is passed (or env
    SPOTIFY_REDIRECT_PORT is set), we use that instead, but the user
    must keep the dashboard list in sync.
    """
    cid, _ = _app_creds()
    if port is None:
        env_port = os.environ.get("SPOTIFY_REDIRECT_PORT")
        port = int(env_port) if env_port else 8765
    redirect_uri = f"http://127.0.0.1:{port}"
    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        # show_dialog=true forces re-consent even if previously authed,
        # so a scope change is acknowledged by the user.
        "show_dialog": "true",
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    print("opening browser for Spotify consent:")
    print(f"  {auth_url}")
    print()
    print(
        f"NOTE: Spotify requires {redirect_uri} to be in the app's Redirect "
        "URIs list in the dashboard. If you see 'INVALID_CLIENT: Invalid "
        "redirect URI', add it at developer.spotify.com/dashboard."
    )
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
        raise RuntimeError(f"Spotify auth failed: {_CodeCatcher.captured_error}")
    if not _CodeCatcher.captured_code:
        raise RuntimeError("Spotify auth timed out before the redirect arrived.")

    tokens = _exchange_code_for_tokens(_CodeCatcher.captured_code, redirect_uri)
    if "refresh_token" not in tokens:
        raise RuntimeError("Spotify returned no refresh_token — unexpected.")
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
