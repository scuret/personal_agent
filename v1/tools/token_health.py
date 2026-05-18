"""Token health check — verify every configured API token is valid.

Pings each provider's lightweight identity / metadata endpoint and reports:

  ✓  valid
  ✗  invalid (with HTTP status / error body summary)
  ⚠  valid-but-flagged (e.g. short-lived Dropbox sl.u. token)
  -  skipped (env var not set)

Each check uses the cheapest read-only call available so this script is
safe to run on a cron without burning quota:

  Anthropic     models.list()                      — free, no token charge
  Todoist       GET /api/v1/projects               — free
  Notion        GET /v1/users/me                   — free, identifies the
                                                     integration's bot user
  GitHub        GET /user                          — free, also surfaces
                                                     X-OAuth-Scopes header
  Brave Search  /res/v1/web/search?q=test&count=1  — 1 quota unit
  YouTube       /youtube/v3/videos?id=dQw4w9WgXcQ  — 1 unit (rickroll-as-
                                                     fixture)
  Dropbox       /users/get_current_account         — free
  Google OAuth  load + refresh-if-needed the cached pickle, check scopes

Run from v1/:
    python -m tools.token_health
"""

from __future__ import annotations

import os
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

import requests  # noqa: E402


def _r(status: str, message: str) -> dict[str, str]:
    return {"status": status, "message": message}


# ─── Per-provider checks ────────────────────────────────────────────────────


def check_anthropic() -> dict[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return _r("skip", "not configured")
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        models = client.models.list()
        n = len(getattr(models, "data", []) or [])
        return _r("ok", f"valid ({n} models accessible)")
    except Exception as e:  # noqa: BLE001
        return _r("fail", f"{type(e).__name__}: {str(e)[:200]}")


def check_todoist() -> dict[str, str]:
    key = os.environ.get("TODOIST_API_KEY", "").strip()
    if not key:
        return _r("skip", "not configured")
    try:
        r = requests.get(
            "https://api.todoist.com/api/v1/projects",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", []) if isinstance(data, dict) else data
            return _r("ok", f"valid ({len(results)} projects visible)")
        return _r("fail", f"HTTP {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        return _r("fail", f"{type(e).__name__}: {e}")


def check_notion() -> dict[str, str]:
    key = os.environ.get("NOTION_INTEGRATION_TOKEN", "").strip()
    if not key:
        return _r("skip", "not configured")
    try:
        r = requests.get(
            "https://api.notion.com/v1/users/me",
            headers={
                "Authorization": f"Bearer {key}",
                "Notion-Version": "2022-06-28",
            },
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            return _r("ok", f"valid (bot: {d.get('name', '?')})")
        return _r("fail", f"HTTP {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        return _r("fail", f"{type(e).__name__}: {e}")


def check_github() -> dict[str, str]:
    key = os.environ.get("GITHUB_TOKEN", "").strip()
    if not key:
        return _r("skip", "not configured")
    try:
        r = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        if r.status_code == 200:
            u = r.json()
            # Classic tokens expose scopes via header; fine-grained don't.
            scopes = r.headers.get("X-OAuth-Scopes", "").strip()
            scope_str = scopes if scopes else "(fine-grained)"
            return _r("ok", f"valid ({u.get('login')} — scopes: {scope_str})")
        return _r("fail", f"HTTP {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        return _r("fail", f"{type(e).__name__}: {e}")


def check_brave() -> dict[str, str]:
    key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        return _r("skip", "not configured")
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            params={"q": "test", "count": 1},
            timeout=10,
        )
        if r.status_code == 200:
            return _r("ok", "valid (1 quota unit consumed)")
        return _r("fail", f"HTTP {r.status_code}: {r.text[:160]}")
    except requests.RequestException as e:
        return _r("fail", f"{type(e).__name__}: {e}")


def check_youtube() -> dict[str, str]:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        return _r("skip", "not configured")
    try:
        # videos.list with a known-good ID — 1 unit, no search overhead.
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"id": "dQw4w9WgXcQ", "part": "id", "key": key},
            timeout=10,
        )
        if r.status_code == 200:
            return _r("ok", "valid (1 quota unit consumed)")
        return _r("fail", f"HTTP {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        return _r("fail", f"{type(e).__name__}: {e}")


def check_dropbox() -> dict[str, str]:
    key = os.environ.get("DROPBOX_ACCESS_TOKEN", "").strip()
    if not key:
        return _r("skip", "not configured")
    try:
        r = requests.post(
            "https://api.dropboxapi.com/2/users/get_current_account",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            name = (d.get("name") or {}).get("display_name", "?")
            email = d.get("email", "?")
            # sl.u. = short-lived, expires ~4h after issue
            if key.startswith("sl.u."):
                return _r(
                    "warn",
                    f"valid ({name}, {email}) — short-lived sl.u. token, "
                    "expires ~4h after issue. Use OAuth refresh flow long-term.",
                )
            return _r("ok", f"valid ({name}, {email})")
        return _r("fail", f"HTTP {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        return _r("fail", f"{type(e).__name__}: {e}")


def check_google_oauth() -> dict[str, str]:
    """Verify the cached Gmail + Calendar OAuth token still works."""
    from core.paths import google_token_path
    token_path = google_token_path()
    if not token_path.exists():
        return _r("skip", f"no cached token at {token_path}")
    try:
        from google.auth.transport.requests import Request

        with token_path.open("rb") as f:
            creds = pickle.load(f)  # noqa: S301 — own-data only
        if creds is None:
            return _r("fail", "pickle deserialized to None")
        scopes = list(creds.scopes or [])
        msg_extra = ""
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                msg_extra = " (just refreshed)"
            except Exception as e:  # noqa: BLE001
                return _r("fail", f"refresh failed: {type(e).__name__}: {e}")
        if not creds.valid:
            return _r("fail", "credentials.valid is False after refresh attempt")
        # Compact scope display: drop the URL prefix
        scope_short = ", ".join(s.rsplit("/", 1)[-1] for s in scopes)
        return _r("ok", f"valid{msg_extra} (scopes: {scope_short})")
    except Exception as e:  # noqa: BLE001
        return _r("fail", f"{type(e).__name__}: {e}")


CHECKS: list[tuple[str, Callable[[], dict[str, Any]]]] = [
    ("ANTHROPIC_API_KEY", check_anthropic),
    ("TODOIST_API_KEY", check_todoist),
    ("NOTION_INTEGRATION_TOKEN", check_notion),
    ("GITHUB_TOKEN", check_github),
    ("BRAVE_SEARCH_API_KEY", check_brave),
    ("YOUTUBE_API_KEY", check_youtube),
    ("DROPBOX_ACCESS_TOKEN", check_dropbox),
    ("Google OAuth (Gmail+Calendar)", check_google_oauth),
]

_SYM = {"ok": "✓", "fail": "✗", "warn": "⚠", "skip": "-"}


def run_checks() -> list[dict[str, Any]]:
    """Public: run every check, return a list of result dicts.

    Each entry: {name, status, message}. Used by the web UI's
    observability panel; the CLI main() also drives from this.
    """
    results: list[dict[str, Any]] = []
    for name, check_fn in CHECKS:
        try:
            r = check_fn()
        except Exception as e:  # noqa: BLE001
            r = _r("fail", f"check itself errored: {type(e).__name__}: {e}")
        results.append({"name": name, **r})
    return results


def main() -> int:
    print(f"=== token health — {datetime.now().isoformat(timespec='seconds')} ===\n")

    failed = 0
    warned = 0
    for r in run_checks():
        sym = _SYM.get(r["status"], "?")
        print(f"  {sym} {r['name']:<32} {r['message']}")
        if r["status"] == "fail":
            failed += 1
        elif r["status"] == "warn":
            warned += 1

    print()
    if failed:
        print(f"{failed} failed — fix before relying on those integrations.")
        return 1
    if warned:
        print(f"{warned} warning(s) — review the notes above.")
    else:
        print("all configured tokens valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
