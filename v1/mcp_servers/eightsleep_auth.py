"""Eight Sleep auth — unofficial API session token handling.

Eight Sleep doesn't publish a developer API. The pyEight library and
the Home Assistant integration both reverse-engineer the same REST
endpoints that the Eight Sleep iOS app uses. We do the same here with
a thin requests-based wrapper.

Env vars:
  EIGHT_EMAIL
  EIGHT_PASSWORD
  EIGHT_TOKEN_PATH  (default: data/eight_token.json)

The login response returns a session token + expiration. We cache it
locally; on expiry, we re-login with email/password. There's no OAuth
refresh-token flow — the credentials themselves are the "refresh
mechanism."

CAVEAT: unofficial API. Eight Sleep can change endpoints without
notice and this whole sub-agent could break. We keep it isolated so a
failure here doesn't crash other sub-agents.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

CLIENT_LOGIN_URL = "https://client-api.8slp.net/v1/login"
DEFAULT_TOKEN_PATH = "./data/eight_token.json"

# Re-login when token is within this window of expiring, to avoid
# a race between "check" and "use."
_REFRESH_LEAD_SECONDS = 300


def _v1_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _token_path() -> Path:
    raw = os.environ.get("EIGHT_TOKEN_PATH", DEFAULT_TOKEN_PATH)
    return Path(raw) if Path(raw).is_absolute() else (_v1_dir() / raw)


def _creds() -> tuple[str, str]:
    email = (os.environ.get("EIGHT_EMAIL") or "").strip()
    pw = (os.environ.get("EIGHT_PASSWORD") or "").strip()
    if not email or not pw:
        raise RuntimeError(
            "EIGHT_EMAIL and EIGHT_PASSWORD must be set in .env. "
            "The Eight Sleep API is unofficial — credentials are sent "
            "directly to their iOS-app login endpoint."
        )
    return email, pw


def _load_cache() -> dict[str, Any] | None:
    p = _token_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(data: dict[str, Any]) -> None:
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        dt = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    return (dt - datetime.now(timezone.utc)).total_seconds() < _REFRESH_LEAD_SECONDS


def _login() -> dict[str, Any]:
    """Hit Eight Sleep's login endpoint, return the session payload."""
    email, pw = _creds()
    r = requests.post(
        CLIENT_LOGIN_URL,
        json={"email": email, "password": pw},
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"eight sleep login HTTP {r.status_code}: {r.text[:300]}"
        )
    body = r.json()
    session = body.get("session") or body
    token = session.get("token")
    user_id = session.get("userId") or body.get("user", {}).get("id")
    expires_in = int(session.get("expirationDate") and 0 or 86400 * 30)
    # The API returns an ISO expirationDate string we should prefer when
    # present; fall back to a 30-day TTL if missing.
    if session.get("expirationDate"):
        try:
            expires_at = session["expirationDate"]
            # validate it's parseable; raises ValueError if not
            datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            ).isoformat()
    else:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat()

    if not token or not user_id:
        raise RuntimeError(
            f"eight sleep login response missing token or userId: {body}"
        )
    return {"token": token, "user_id": user_id, "expires_at": expires_at}


def get_session() -> dict[str, Any]:
    """Return a valid session dict with `token` and `user_id`. Refreshes
    via re-login when the cache is missing/expired."""
    cache = _load_cache()
    if cache and not _is_expired(cache.get("expires_at")):
        return cache
    fresh = _login()
    _save_cache(fresh)
    return fresh


def auth_headers() -> dict[str, str]:
    s = get_session()
    return {
        "Session-Token": s["token"],
        "Accept": "application/json",
        "User-Agent": "personal-agent/1.0 (Apple iOS/EightSleep client compatible)",
    }


def user_id() -> str:
    return str(get_session()["user_id"])


def main() -> int:
    """CLI: verify creds + login + dump session metadata."""
    from dotenv import load_dotenv

    load_dotenv()
    try:
        _creds()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        session = get_session()
    except Exception as e:  # noqa: BLE001
        print(f"error: login failed: {e}", file=sys.stderr)
        return 1
    print(f"ok. session cached at {_token_path()}")
    print(f"user_id: {session['user_id']}")
    print(f"expires_at: {session['expires_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
