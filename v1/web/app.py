"""personal_agent web admin UI — FastAPI entrypoint.

Single uvicorn process, bound to 127.0.0.1:8770. Wraps the existing
modules (memory.store, scheduler.triggers, agent_host, tools/*) — no
IPC, just Python imports. Templating is Jinja2 + HTMX + Tailwind via
CDN (no Node toolchain).

Run for local dev:
    uvicorn web.app:app --host 127.0.0.1 --port 8770 --reload

Production (LaunchAgent):
    uvicorn web.app:app --host 127.0.0.1 --port 8770
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

V1_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = V1_DIR / "web" / "templates"
STATIC_DIR = V1_DIR / "web" / "static"

# Bound to 127.0.0.1 only — no LAN exposure, no auth needed for v1.
# If you ever expose this to a network, add session-cookie auth at
# this middleware boundary first.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup + shutdown hooks. v1 has nothing to initialize at startup;
    routes lazily construct MemoryStore instances on first use."""
    yield


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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

    # Import + register routes here (not at module top) so the test
    # harness can construct the app without firing every dependency.
    from web.routes import (  # noqa: E402
        chat,
        config,
        conversations,
        daemon,
        facts,
        home,
        observability,
        reminders,
        triggers,
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

    @app.exception_handler(404)
    async def _not_found(request: Request, _exc):
        return HTMLResponse(
            templates.get_template("404.html").render({"request": request}),
            status_code=404,
        )

    return app


app = make_app()


def main() -> None:
    """CLI: `python -m web.app` — handy for `--run-now`-style local
    starts without typing the uvicorn command."""
    import uvicorn

    uvicorn.run(
        "web.app:app",
        host=os.environ.get("WEB_HOST", DEFAULT_HOST),
        port=int(os.environ.get("WEB_PORT", DEFAULT_PORT)),
        reload=False,
    )


if __name__ == "__main__":
    main()
