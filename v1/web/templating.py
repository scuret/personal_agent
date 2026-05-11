"""Shared Jinja2Templates instance.

Lives in its own module to break the circular import between web.app
(which builds the FastAPI app + registers routes) and web/routes/*
(which need the templates renderer at module-top-level). Both sides
import from here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
