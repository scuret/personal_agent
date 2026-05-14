"""Shared Google OAuth helper for the Gmail and Calendar MCP servers.

One token, multiple services. The OAuth flow is driven by:

  GOOGLE_OAUTH_CREDENTIALS_PATH  (default: config/credentials.json)
      The OAuth client JSON downloaded from Google Cloud Console.

  GOOGLE_OAUTH_TOKEN_PATH        (default: data/google_token.pickle)
      Where the cached refresh-token-bearing credentials live after the
      first auth dance. Created on first auth, refreshed automatically
      thereafter.

SCOPES is the union of every Google API surface v1 uses, so a single
auth dance covers Gmail + Calendar at once. Adding a new scope later
forces a re-auth (we detect scope mismatch and rerun the flow).

Usage:
    from mcp_servers.google_auth import build_service
    gmail = build_service("gmail", "v1")
    calendar = build_service("calendar", "v3")

First-time setup (do this once before running agent_host):
    python -m mcp_servers.google_auth
"""

from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Union of every Google API scope this app uses. Keep tight — we only
# request what we actually call.
#
# - gmail.modify: read messages, create drafts, archive (modify INBOX
#   label), mark read. Does NOT include the explicit `gmail.send` scope;
#   even so, the application enforces "no send" by not exposing a send
#   tool and by the PreToolUse send-block hook in agent_host.
# - calendar.events: read AND write events on the user's calendars. This
#   replaces the earlier calendar.readonly — calendar.events includes
#   the read surface so we don't need both. Doesn't grant access to
#   create/delete the calendars themselves, just events on them.
# - drive: full read/write access to Drive files. We use this for the
#   Drive sub-agent's search, list, read, share-link tools, and as the
#   transport for creating new Docs and Sheets (which live in Drive).
#   `drive.file` was rejected because it only sees app-created files,
#   which makes "find my X spreadsheet" impossible.
# - documents: full read/write to Google Docs the user owns or has
#   access to. Powers docs_read/append/replace/create.
# - spreadsheets: full read/write to Sheets the user owns or has access
#   to. Powers sheets_read_range/append/update/create.
SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _v1_dir() -> Path:
    """Return the v1/ directory regardless of where this is imported from."""
    return Path(__file__).resolve().parent.parent


def _credentials_path() -> Path:
    raw = os.environ.get("GOOGLE_OAUTH_CREDENTIALS_PATH", "./config/credentials.json")
    return Path(raw) if Path(raw).is_absolute() else (_v1_dir() / raw)


def _token_path() -> Path:
    raw = os.environ.get("GOOGLE_OAUTH_TOKEN_PATH", "./data/google_token.pickle")
    return Path(raw) if Path(raw).is_absolute() else (_v1_dir() / raw)


def _load_cached_credentials() -> Credentials | None:
    path = _token_path()
    if not path.exists():
        return None
    with path.open("rb") as f:
        return pickle.load(f)  # noqa: S301 — own-data only, never untrusted input


def _save_credentials(creds: Credentials) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        pickle.dump(creds, f)
    # Pickle holds live Google OAuth refresh + access tokens. Owner-
    # only file perms. ROADMAP "Security enhancements" H1.
    os.chmod(path, 0o600)


def _scopes_satisfied(creds: Credentials, required: list[str]) -> bool:
    have = set(creds.scopes or [])
    return all(s in have for s in required)


def get_credentials(required_scopes: list[str] | None = None) -> Credentials:
    """Return valid Credentials, running the OAuth flow if needed.

    If a cached token exists with all required scopes, use it (refreshing
    if expired). Otherwise run the local-server OAuth flow, which pops a
    browser for the user to authorize.

    Pass `required_scopes` to demand a specific subset. Defaults to the
    full SCOPES list (the union we'll need across all services).
    """
    required = required_scopes or SCOPES
    creds = _load_cached_credentials()

    # Cached but missing one or more scopes → drop and re-auth.
    if creds and not _scopes_satisfied(creds, required):
        creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
            return creds
        except Exception:  # noqa: BLE001 — refresh failure is recoverable via re-auth
            creds = None

    # Run the interactive OAuth flow.
    creds_path = _credentials_path()
    if not creds_path.exists():
        raise FileNotFoundError(
            f"OAuth client credentials missing at {creds_path}. Download the "
            "OAuth 2.0 Client ID JSON from Google Cloud Console (Credentials → "
            "Download JSON) and save it there."
        )
    # `from_client_secrets_file` requires SCOPES to be the full set we want
    # the token to grant; we always pass the union to keep the token broad
    # enough for any service the app uses.
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)
    _save_credentials(creds)
    return creds


def build_service(api: str, version: str) -> Any:
    """Build an authenticated google-api-python-client service.

    Examples:
        gmail = build_service("gmail", "v1")
        calendar = build_service("calendar", "v3")
    """
    creds = get_credentials()
    # cache_discovery=False avoids a noisy file-cache warning under newer
    # google-api-python-client versions; we don't need disk caching here.
    return build(api, version, credentials=creds, cache_discovery=False)


def main() -> None:
    """CLI entrypoint: run the OAuth flow once before first agent_host start.

    `python -m mcp_servers.google_auth`
    """
    creds_path = _credentials_path()
    token_path = _token_path()
    print(f"credentials: {creds_path}")
    print(f"token cache: {token_path}")
    print(f"requesting scopes: {SCOPES}")
    print()
    if token_path.exists():
        print("a cached token already exists. continue to refresh / re-auth.")
    creds = get_credentials()
    print()
    print(f"ok. token saved to {token_path}.")
    print(f"granted scopes: {creds.scopes}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
