"""CSRF defense for the local web UI.

The web UI binds to 127.0.0.1 (per ROADMAP H3) and has no auth — both
are intentional choices for a single-user local admin tool. But
loopback binding alone does NOT defend against CSRF:

  * **Local malicious HTML.** Any browser tab the principal opens with
    a file:// or http://localhost:<port> URL can issue cross-origin
    POSTs to 127.0.0.1:8780. Even an attacker-controlled email
    rendered in Mail.app's preview can include `<img>` / `<form>`
    elements that hit our state-changing endpoints.
  * **DNS rebinding.** A public domain can be configured to return
    127.0.0.1 for short-TTL queries after the initial page loads;
    once the browser's same-origin policy considers itself "same
    origin" with the attacker's page, fetch() POSTs to our localhost
    bind succeed.

The defense: validate the `Origin` (preferred) or `Referer` header on
every state-changing request and reject anything that doesn't match
our own origin. Browsers send `Origin` on cross-origin POSTs even
without CORS headers in the response, so this is effective even
against fetch() from a different page.

Tradeoffs:
  * Curl + scripted POSTs without an Origin header are rejected. That
    means automation against the local UI now has to set
    `--header "Origin: http://127.0.0.1:8780"` (one extra flag,
    documented). Worth it.
  * Server-to-server callbacks (none in v1) would need allowlisting.
    The `WEB_ALLOWED_ORIGINS` env var (comma-separated) is the escape
    hatch for forkers running behind a reverse proxy / Tailscale.

State-changing = any HTTP method other than GET/HEAD/OPTIONS.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _allowed_origins() -> set[str]:
    """Build the allowed-origin set.

    Always includes `http://127.0.0.1:<WEB_PORT>` (where WEB_PORT
    defaults to 8780) and `http://localhost:<WEB_PORT>` so the
    browser's loopback resolution is covered. Extra origins can be
    added via `WEB_ALLOWED_ORIGINS` (comma-separated full origins:
    e.g. `https://agent.example.com,https://other.tld`) for forkers
    who run the UI behind a reverse proxy.
    """
    port = (os.environ.get("WEB_PORT") or "8780").strip() or "8780"
    out = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }
    raw = (os.environ.get("WEB_ALLOWED_ORIGINS") or "").strip()
    if raw:
        for chunk in raw.split(","):
            o = chunk.strip().rstrip("/")
            if o:
                out.add(o)
    return out


def _origin_allowed(origin: str, allowed: set[str]) -> bool:
    """Exact-match check against the allowed origin set."""
    return origin in allowed


def _referer_allowed(referer: str, allowed: set[str]) -> bool:
    """Compare the Referer's origin (scheme://host:port) to the allowed set."""
    try:
        parsed = urlparse(referer)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    ref_origin = f"{parsed.scheme}://{parsed.netloc}"
    return ref_origin in allowed


class CSRFMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests with a missing or unrecognized
    Origin/Referer header.

    Order of checks per request:
      1. Safe method (GET/HEAD/OPTIONS) → pass.
      2. `Origin` header present → must be in the allowed set.
      3. Else `Referer` header present → its origin must be in the set.
      4. Else (no Origin AND no Referer) → reject. Same-origin browser
         POSTs always carry at least one; a request with neither is
         either a curl / scripted POST (which our docs ask to set
         `--header "Origin: http://127.0.0.1:8780"`) or an attacker
         specifically stripping headers, both of which we want to deny
         by default.

    Returns JSON 403 on rejection so XHR / fetch() callers see a clean
    error.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        allowed = _allowed_origins()
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")

        if origin:
            if not _origin_allowed(origin, allowed):
                return _csrf_reject(
                    f"Origin {origin!r} not in allowed set. Add to "
                    f"WEB_ALLOWED_ORIGINS if this is a legitimate caller, "
                    f"or set Origin: http://127.0.0.1:8780 for local curl."
                )
        elif referer:
            if not _referer_allowed(referer, allowed):
                return _csrf_reject(
                    f"Referer {referer!r} doesn't match the local web UI origin."
                )
        else:
            return _csrf_reject(
                "no Origin or Referer header. Browsers send these on "
                "same-origin POSTs; if you're using curl, add "
                "--header 'Origin: http://127.0.0.1:8780'."
            )

        return await call_next(request)


def _csrf_reject(detail: str) -> JSONResponse:
    return JSONResponse({"error": f"CSRF: {detail}"}, status_code=403)
