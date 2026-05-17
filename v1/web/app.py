"""personal_agent web admin UI — FastAPI entrypoint.

Single uvicorn process, bound to 127.0.0.1:8780. Wraps the existing
modules (memory.store, scheduler.triggers, agent_host, tools/*) — no
IPC, just Python imports. Templating is Jinja2 + HTMX + Tailwind via
CDN (no Node toolchain).

Run for local dev:
    uvicorn web.app:app --host 127.0.0.1 --port 8780 --reload

Production (LaunchAgent):
    uvicorn web.app:app --host 127.0.0.1 --port 8780
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from web.templating import templates  # noqa: E402

V1_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = V1_DIR / "web" / "static"
UPLOADS_DIR = V1_DIR / "data" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Bound to 127.0.0.1 only — no LAN exposure, no auth needed for v1.
# If you ever expose this to a network, add session-cookie auth at
# this middleware boundary first.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup + shutdown hooks. v1 has nothing to initialize at startup;
    routes lazily construct MemoryStore instances on first use."""
    yield


def make_app() -> FastAPI:
    app = FastAPI(
        title="personal_agent admin",
        description="Local admin UI for the personal_agent program.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,           # no swagger — keeps surface area minimal
        redoc_url=None,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # Serve user-uploaded chat attachments so the browser can render
    # thumbnails of the image the user just sent. Bound to 127.0.0.1
    # via the parent server, so this is single-user-only.
    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

    # Import + register routes here (not at module top) so the test
    # harness can construct the app without firing every dependency.
    from web.routes import (
        about,
        chat,
        config,
        conversations,
        daemon,
        facts,
        home,
        install,
        learning,
        observability,
        reminders,
        settings,
        settings_transports,
        triggers,
        wizard,
    )

    app.include_router(home.router)
    app.include_router(chat.router)
    app.include_router(observability.router)
    app.include_router(conversations.router)
    app.include_router(facts.router)
    app.include_router(reminders.router)
    app.include_router(triggers.router)
    app.include_router(daemon.router)
    app.include_router(config.router)
    app.include_router(settings.router)
    app.include_router(settings_transports.router)
    app.include_router(wizard.router)
    app.include_router(install.router)
    app.include_router(about.router)
    app.include_router(learning.router)

    @app.exception_handler(404)
    async def _not_found(request: Request, _exc):
        return HTMLResponse(
            templates.get_template("404.html").render({"request": request}),
            status_code=404,
        )

    return app


app = make_app()


def _resolve_host() -> str:
    """Pick the bind address.

    The web UI has no authentication and no CSRF protection (see the
    Privacy & security profile in README.md). We hard-bind to
    127.0.0.1 to keep state-changing endpoints unreachable from the
    LAN. The `WEB_HOST` env var is honored only when the user has
    also opted into `WEB_ALLOW_LAN=1` — a deliberate two-step so a
    stray `WEB_HOST=0.0.0.0` typo doesn't silently expose the UI.

    See ROADMAP "Security enhancements" H3 for the full rationale.
    """
    requested = os.environ.get("WEB_HOST", "").strip()
    if not requested or requested == DEFAULT_HOST:
        return DEFAULT_HOST
    allow_lan = os.environ.get("WEB_ALLOW_LAN", "").strip().lower() in {
        "1", "true", "yes", "y",
    }
    if not allow_lan:
        print(
            f"[web] ignoring WEB_HOST={requested!r}: set WEB_ALLOW_LAN=1 "
            f"to bind to a non-loopback address. Sticking to {DEFAULT_HOST}.",
            file=sys.stderr,
        )
        return DEFAULT_HOST
    print(
        f"[web] ⚠ WEB_ALLOW_LAN=1 + WEB_HOST={requested!r} — UI is now "
        "reachable beyond loopback. There is NO authentication and NO "
        "CSRF protection. Put it behind a firewall or reverse proxy.",
        file=sys.stderr,
    )
    return requested


def main() -> None:
    """CLI: `python -m web.app` — handy for `--run-now`-style local
    starts without typing the uvicorn command."""
    import uvicorn

    uvicorn.run(
        "web.app:app",
        host=_resolve_host(),
        port=int(os.environ.get("WEB_PORT", DEFAULT_PORT)),
        reload=False,
    )


if __name__ == "__main__":
    main()
