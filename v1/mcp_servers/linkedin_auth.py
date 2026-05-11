"""LinkedIn OAuth flow (OpenID Connect + w_member_social).

LinkedIn's standard personal-tier OAuth does NOT issue refresh tokens —
that's gated behind Marketing Developer Platform approval. Standard
access tokens last 60 days, then the user has to re-consent. This
module caches the access token + expiry and tells the user when it's
time to re-run the CLI consent.

Env vars (set in .env):

  LINKEDIN_CLIENT_ID       App's client ID, from the LinkedIn developer portal.
  LINKEDIN_CLIENT_SECRET   App's client secret.
  LINKEDIN_REDIRECT_PORT   Optional. Default 8768.

Token cache:

  LINKEDIN_TOKEN_PATH      Default: data/linkedin_token.json. Stores
                           {access_token, expires_at, scope, sub}.

App setup at www.linkedin.com/developers/apps (once, before CLI):

  1. Create app → fill name, logo, LinkedIn Page (any company page you
     admin; required by LinkedIn). If you don't admin a page, create
     a personal showcase page first.
  2. Auth tab → Authorized redirect URLs:
       http://127.0.0.1:8768
     (exact match, no trailing slash).
  3. Products tab → request:
       - "Sign In with LinkedIn using OpenID Connect"  (auto-approved)
       - "Share on LinkedIn"  (auto-approved; enables w_member_social)
  4. Auth tab → copy Client ID + Client Secret → .env.

First-time CLI consent at the Mac:

  python -m mcp_servers.linkedin_auth

Browser opens to linkedin.com/oauth — click Allow. The local helper
catches the redirect, exchanges code → access token, writes the cache.
"""

from __future__ import annotations

import json
import os
import secrets
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

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
DEFAULT_TOKEN_PATH = "./data/linkedin_token.json"
DEFAULT_REDIRECT_PORT = 8768

# Standard LinkedIn access tokens last ~60 days. Warn 5 days before
# expiry so the user has time to re-auth before the agent breaks.
_REFRESH_LEAD_SECONDS = 5 * 24 * 3600

# OpenID Connect (profile) + post-creation. These cover the only
# meaningful surface a personal LinkedIn app can access.
SCOPES: list[str] = [
    "openid",
    "profile",
    "email",
    "w_member_social",
]


def _v1_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _token_path() -> Path:
    raw = os.environ.get("LINKEDIN_TOKEN_PATH", DEFAULT_TOKEN_PATH)
    return Path(raw) if Path(raw).is_absolute() else (_v1_dir() / raw)


def _redirect_port() -> int:
    raw = os.environ.get("LINKEDIN_REDIRECT_PORT")
    if raw and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            pass
    return DEFAULT_REDIRECT_PORT


def _app_creds() -> tuple[str, str]:
    cid = (os.environ.get("LINKEDIN_CLIENT_ID") or "").strip()
    sec = (os.environ.get("LINKEDIN_CLIENT_SECRET") or "").strip()
    if not cid or not sec:
        raise RuntimeError(
            "LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET must be set. "
            "Create an app at www.linkedin.com/developers/apps, then copy the "
            "Client ID and Client Secret into .env."
        )
    return cid, sec


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
    # LinkedIn personal apps issue 60-day tokens; expires_in is in seconds.
    seconds = int(payload.get("expires_in", 60 * 24 * 3600))
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        dt = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    return (dt - datetime.now(timezone.utc)).total_seconds() < _REFRESH_LEAD_SECONDS


def _exchange_code_for_tokens(code: str, redirect_uri: str) -> dict[str, Any]:
    cid, sec = _app_creds()
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": cid,
            "client_secret": sec,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Pull the OIDC userinfo claim — we cache `sub` to use as the URN
    in post-creation calls."""
    resp = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_access_token() -> str:
    """Return a non-expired access token. LinkedIn standard apps don't
    issue refresh tokens — if the cached token is expired (or within
    the warn-window), instruct the user to re-run the CLI consent."""
    cache = _load_cache()
    if not cache or not cache.get("access_token"):
        raise RuntimeError(
            "no cached LinkedIn token. run "
            "`python -m mcp_servers.linkedin_auth` at the Mac to grant "
            "access."
        )
    if _is_expired(cache.get("expires_at")):
        raise RuntimeError(
            "cached LinkedIn token is expired (or about to expire). "
            "Standard LinkedIn personal apps don't issue refresh tokens; "
            "re-run `python -m mcp_servers.linkedin_auth` to get a new "
            "60-day token."
        )
    return cache["access_token"]


def get_user_urn() -> str:
    """Return the LinkedIn person URN for the cached account.

    Used as the `author` field on post-creation requests. Cached on
    the token cache to avoid re-fetching userinfo on every post.
    """
    cache = _load_cache() or {}
    sub = cache.get("sub")
    if not sub:
        raise RuntimeError(
            "no cached LinkedIn `sub` (user URN). Re-run "
            "`python -m mcp_servers.linkedin_auth` to refresh the cache."
        )
    return f"urn:li:person:{sub}"


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
    cid, _ = _app_creds()
    port = _redirect_port()
    redirect_uri = f"http://127.0.0.1:{port}"
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
    }
    auth_url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    print(f"redirect URI for this run: {redirect_uri}")
    print(
        "NOTE: LinkedIn requires this exact URI to be in your app's "
        "Authorized redirect URLs at www.linkedin.com/developers/apps. If "
        "you see a redirect_uri error, add it and re-run."
    )
    print()
    print("opening browser for LinkedIn consent:")
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
        raise RuntimeError(f"LinkedIn auth failed: {_CodeCatcher.captured_error}")
    if not _CodeCatcher.captured_code:
        raise RuntimeError("LinkedIn auth timed out before the redirect arrived.")

    tokens = _exchange_code_for_tokens(_CodeCatcher.captured_code, redirect_uri)
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError(
            f"LinkedIn returned no access_token. response: {tokens}"
        )

    # Fetch userinfo immediately so we cache the URN for post creation.
    try:
        userinfo = _fetch_userinfo(access_token)
    except Exception as e:  # noqa: BLE001
        print(
            f"warning: userinfo fetch failed ({e}). post tools won't work "
            "without a cached `sub`.",
            file=sys.stderr,
        )
        userinfo = {}

    cache = {
        "access_token": access_token,
        "expires_at": _expires_at_from(tokens),
        "scope": tokens.get("scope", " ".join(SCOPES)),
        "sub": userinfo.get("sub"),
        "name": userinfo.get("name"),
        "email": userinfo.get("email"),
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
    if existing and existing.get("access_token"):
        print("an existing token is cached. re-running will overwrite it.")
    print()
    run_interactive_auth()
    print()
    cache = _load_cache() or {}
    print(f"ok. token saved to {_token_path()}.")
    print(f"granted scope: {cache.get('scope', '?')}")
    print(f"account: {cache.get('name', '?')} <{cache.get('email', '?')}>")
    print(f"sub (urn): {cache.get('sub', '?')}")
    print(f"expires at: {cache.get('expires_at', '?')}")
    print(
        "LinkedIn personal-tier tokens don't refresh — re-run this in "
        "~55 days to keep the agent connected."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
