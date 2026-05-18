"""Single source of truth for filesystem paths the agent reads + writes.

Until now every module computed `V1_DIR / "data"` / `V1_DIR / "config"`
on its own. That works fine for the dev workflow where everything lives
next to the source, but it can't accommodate a bundled distribution
(the macOS GUI app) where:

  * code lives at  `PersonalAgentApp.app/Contents/Resources/v1/`
  * state lives at `~/Library/Application Support/personal_agent/`

This module centralizes the resolution. Two roots:

  * `source_dir()` — where the code + bundled assets live (web templates,
    static, launch_agents plist templates, docs/setup images, .env.example).
    Always equals the v1 source tree.
  * `home_dir()` — where the user's state lives (`.env`, `data/`, `config/`).
    Defaults to `source_dir()` for back-compat with the dev workflow; can
    be relocated by setting the `PERSONAL_AGENT_HOME` environment variable.

The GUI app's launcher sets `PERSONAL_AGENT_HOME` to the Application
Support path before spawning the Python backend. The dev workflow leaves
it unset, so everything keeps working unchanged.

Every helper returns an absolute `Path`. None of them create the directory
— callers do that with `mkdir(parents=True, exist_ok=True)` at write time
so this module stays import-side-effect-free.
"""

from __future__ import annotations

import os
from pathlib import Path

# Resolve once at import. This file lives at v1/core/paths.py, so
# parent.parent is the v1/ source tree.
_SOURCE_DIR: Path = Path(__file__).resolve().parent.parent


def source_dir() -> Path:
    """The v1/ source tree. Bundled assets (templates, static files,
    .env.example, launch_agents/*.plist templates, docs/setup/*.png)
    live here. Does NOT depend on PERSONAL_AGENT_HOME — these travel
    with the code in a bundled distribution."""
    return _SOURCE_DIR


def home_dir() -> Path:
    """Root of user state — `.env`, `data/`, `config/`.

    Resolution order:
      1. `PERSONAL_AGENT_HOME` env var (used by the macOS GUI app)
      2. Otherwise, `source_dir()` so the dev workflow keeps working
         (`v1/.env`, `v1/data/...`, `v1/config/...`)

    Set the env var to an absolute path. A relative path is resolved
    against the current working directory; we don't second-guess that.
    """
    raw = os.environ.get("PERSONAL_AGENT_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _SOURCE_DIR


# ─── Top-level user-state paths ──────────────────────────────────────────────


def data_dir() -> Path:
    """User-writable data: SQLite DB, audit log, cached tokens, log files,
    chat uploads, install-progress marker. Owner-only (0o600 on files, 0o700
    on dirs) per ROADMAP H1."""
    return home_dir() / "data"


def config_dir() -> Path:
    """User-editable config: personality.md, triggers.yaml, credentials.json."""
    return home_dir() / "config"


def env_path() -> Path:
    """The `.env` the daemon reads at startup + the web UI rewrites
    on save."""
    return home_dir() / ".env"


def env_example_path() -> Path:
    """`.env.example` is a bundled asset, not user state — it ships
    with the source and is used to seed a fresh `.env`."""
    return source_dir() / ".env.example"


def launch_agents_template_dir() -> Path:
    """Where the LaunchAgent plist templates + install.sh live. These
    are source assets; rendered plists land under
    ~/Library/LaunchAgents/ separately."""
    return source_dir() / "launch_agents"


# ─── Common nested paths ─────────────────────────────────────────────────────


def db_path() -> Path:
    """The SQLite memory store. WAL companions (-wal/-shm) sit beside it."""
    return data_dir() / "memory.sqlite"


def uploads_dir() -> Path:
    """`data/uploads/<conversation_id>/` for chat image attachments."""
    return data_dir() / "uploads"


def credentials_path() -> Path:
    """`config/credentials.json` — Google OAuth client JSON. Default;
    overridable via the existing `GOOGLE_OAUTH_CREDENTIALS_PATH` env var
    (so users who already have it elsewhere don't break)."""
    raw = os.environ.get("GOOGLE_OAUTH_CREDENTIALS_PATH", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (source_dir() / p).resolve()
    return config_dir() / "credentials.json"


def personality_path() -> Path:
    """`config/personality.md` — the agent's voice + hard rules."""
    return config_dir() / "personality.md"


def triggers_yaml_path() -> Path:
    """`config/triggers.yaml` — scheduled brief times, email-watch
    config, expected-arrivals watches."""
    return config_dir() / "triggers.yaml"


def google_token_path() -> Path:
    """Cached Google OAuth token pickle. Overridable via
    `GOOGLE_OAUTH_TOKEN_PATH` for back-compat."""
    raw = os.environ.get("GOOGLE_OAUTH_TOKEN_PATH", "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (source_dir() / p).resolve()
    return data_dir() / "google_token.pickle"


def install_progress_path() -> Path:
    """`.install_progress.json` — wizard's resume-mid-flow marker."""
    return data_dir() / ".install_progress.json"


def oauth_token_path(provider: str, env_var: str | None = None) -> Path:
    """Cached refresh-token file for a provider. Per-provider env vars
    (DROPBOX_TOKEN_PATH, SPOTIFY_TOKEN_PATH, CANVA_TOKEN_PATH,
    LINKEDIN_TOKEN_PATH, EIGHT_TOKEN_PATH) take precedence so users who
    already have them set don't break. Default lives under `data/`.

    Args:
      provider: short name (`dropbox`, `spotify`, `canva`, `linkedin`, `eight`).
      env_var: env var to check first; if omitted, derived as
               `<PROVIDER>_TOKEN_PATH`.
    """
    var = env_var or f"{provider.upper()}_TOKEN_PATH"
    raw = os.environ.get(var, "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (source_dir() / p).resolve()
    # Convention matches the existing token filenames.
    filename_by_provider = {
        "dropbox": "dropbox_token.json",
        "spotify": "spotify_token.json",
        "canva": "canva_token.json",
        "linkedin": "linkedin_token.json",
        "eight": "eight_token.json",
    }
    fname = filename_by_provider.get(provider, f"{provider}_token.json")
    return data_dir() / fname
