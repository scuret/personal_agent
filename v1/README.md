# Personal Agent — v1

A personal AI agent that runs as a daemon on your computer, talks to you over **iMessage** or **Telegram** (your choice at install time), and helps with email, tasks, calendar, files, search, and more.

This directory holds the v1 implementation. The historical design spec is in `../pre_requirements.md` (kept for reference; many decisions diverge from it).

## Choosing a transport

Set `RELAY_TRANSPORT` in `.env` to either:

- **`imessage`** — macOS-only. Polls `~/Library/Messages/chat.db` and sends via AppleScript. Requires Full Disk Access + Automation permissions for the daemon. Native iPhone integration; the agent appears as a "Note to Self" thread (or a regular contact in `contact` mode).
- **`telegram`** — Cross-platform. The agent runs as a bot you create via `@BotFather`; only allowlisted Telegram user IDs can talk to it. Works from any Mac, Linux, or Windows host with Python — no iMessage / chat.db dependency, and no iOS Focus / DND quirks.

Switch transports any time by editing `.env` and restarting the relay (`launchctl kickstart -k gui/$(id -u)/com.personal-agent.relay`). Only one runs at a time. The interactive installer (`./install.sh`) walks you through choosing one.

---

## Capabilities at a glance

The agent is a single Claude reasoning loop with nineteen in-process MCP sub-agents. You text it, it picks the right tools.

| Sub-agent | What it does | Auth | Free? |
|---|---|---|---|
| **memory** | Conversation archive + extracted facts + audit log | Local SQLite | ✅ free |
| **archive** | Aggregate analytics — counts, top tools, activity by hour/day | Local SQLite | ✅ free |
| **gmail** | Search, read, draft, archive. **Never sends.** | Google OAuth | ✅ free |
| **calendar** | Read events, search, free/busy, **create / update / delete** | Google OAuth | ✅ free |
| **drive** | Search, browse folders, read text files, create share link | Google OAuth | ✅ free |
| **docs** | Read, append, find-and-replace, create new docs | Google OAuth | ✅ free |
| **sheets** | Read range, append rows, update range, create | Google OAuth | ✅ free |
| **todoist** | List/create/update/complete tasks | API token | ✅ free |
| **notion** | Search, read, query DBs, create page, append | Integration token | ✅ free |
| **github** | Repos, issues, PRs, commits, search, create issue | PAT | ✅ free |
| **weather** | Current + N-day forecast (Open-Meteo) | None needed | ✅ free |
| **vision** | Describe iPhone-attached images (HEIC auto-converted) | Anthropic API | metered |
| **web** | Brave Search + URL fetch | API key | ✅ 2K/mo free |
| **youtube** | Search + video/channel metadata | API key | ✅ 10K units/day |
| **dropbox** | Search, list, read text, share-link (OAuth refresh flow) | OAuth refresh | ✅ free |
| **spotify** | Search, playback, queue, playlists, devices | OAuth refresh | ✅ free (playback needs Premium) |
| **wikipedia** | Search, summary, full article extract | None needed | ✅ free |
| **reddit** | Subreddit top/hot, search, post + comments | None (public read) | ✅ free |
| **reminders** | Schedule "remind me at 4pm to..." | None needed | ✅ free |

Plus: **scheduled morning brief** (~7:30 AM) and **Sunday weekly review** (8 PM) auto-pushed to iMessage.

---

## Web admin UI

A local-only web UI ships alongside the daemons. Open
`http://127.0.0.1:8780` (auto-started by the `com.personal-agent.webui`
LaunchAgent) and you get:

- **Dashboard** — daemon status, last-24h spend, pending reminders, recent conversations, upcoming brief fires, one-click trigger buttons
- **Chat** — talk to the agent in a browser. Streamed SSE responses, conversation continuity (4h gap window), shared archive with iMessage / Telegram / scheduler
- **History** — browse + search the conversation archive; per-conversation message thread with tool calls expanded
- **Observability** — cost report, behavioral analytics (activity by hour/day, sub-agent usage, slow turns), token-health check, live-tailed daemon logs (SSE)
- **Config** — in-browser editors for `triggers.yaml` (live reload, no restart), `personality.md` (restart required), `.env` (secret-masked, restart required)
- **Facts + Reminders** — read-only viewers for now (CRUD in Phase 2 of the UI roadmap)

Stack: FastAPI + Jinja2 + HTMX + Tailwind via CDN. No Node toolchain,
no build step — clone the repo and it just runs after `./install.sh`.
Bound to `127.0.0.1` only; no auth boundary needed.

Manual dev start (without the LaunchAgent):

```bash
.venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8780 --reload
```

---

## Safety contract

Three hard rules, enforced in three places (system prompt + tool surface + SDK pre-tool hook):

1. **Never auto-send email.** No `send_email` tool exists. Drafts go to Gmail Drafts; you send manually.
2. **Never modify shared external state without confirmation.** Reading is free; writing requires explicit user direction in the conversation.
3. **All Claude API traffic is logged locally** (`data/memory.sqlite`, `api_events` table) so you can audit what was sent.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Mac (LaunchAgents auto-start everything below)             │
│                                                             │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────────┐   │
│  │ iMessage relay │  │  Scheduler     │  │ Log rotation │   │
│  │ (chat.db poll) │  │ (briefs +      │  │ (daily 03:00)│   │
│  │                │  │  reminders)    │  │              │   │
│  └────────┬───────┘  └────────┬───────┘  └──────────────┘   │
│           │                   │                             │
│           ▼                   ▼                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │   Agent host (Python, Claude Agent SDK)              │   │
│  │   • personality system prompt                        │   │
│  │   • injected facts from memory                       │   │
│  │   • PreToolUse hook blocks "send" patterns           │   │
│  │   • tools=[] + strict_mcp_config=True (isolation)    │   │
│  └─┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬─┘   │
│    │    │    │    │    │    │    │    │    │    │    │     │
│  memory todoist gmail cal weather vision notion gh web yt dropbox  │
│   ↓     ↓    ↓     ↓    ↓     ↓     ↓    ↓   ↓   ↓     ↓    │
│  SQLite  Todoist  Google APIs       Anthropic  Brave Google Dropbox│
│         REST                                              + reminders │
└─────────────────────────────────────────────────────────────┘
```

---

## Layout

```
v1/
├── agent_host.py             # Main entry: Claude Agent SDK loop
├── system_prompt.py          # Personality + memory injection
├── relay/
│   └── imessage_relay.py     # Polls Messages.app, sends back
├── scheduler/
│   └── triggers.py           # Briefs + weekly review + reminder firing
├── mcp_servers/
│   ├── memory_server.py      # archive + fact extraction + recall
│   ├── gmail_server.py       # read, search, draft, archive (NO send)
│   ├── calendar_server.py    # list, search, availability (read-only)
│   ├── todoist_server.py     # CRUD, daily/overdue filters
│   ├── notion_server.py      # search, read, query DBs, create, append
│   ├── github_server.py      # repos, issues, PRs, search
│   ├── weather_server.py     # current + N-day forecast
│   ├── vision_server.py      # image analysis (HEIC → JPEG via sips)
│   ├── web_server.py         # Brave Search + URL fetch
│   ├── youtube_server.py     # search + video/channel metadata
│   ├── dropbox_server.py     # search, list, read text, share
│   ├── reminders_server.py   # schedule + list + cancel reminders
│   └── google_auth.py        # shared OAuth helper for gmail+calendar
├── memory/
│   └── store.py              # SQLite layer: archive + audit + facts + state + reminders
├── tools/
│   ├── cost_report.py        # Anthropic spend / token usage report
│   └── rotate_logs.py        # Daily log rotation
├── web/                       # local admin UI (FastAPI + Jinja2 + HTMX)
│   ├── app.py                 # FastAPI app + route registration
│   ├── sessions.py            # ClaudeSDKClient pool for chat continuity
│   ├── daemon_control.py      # launchctl wrappers (status, restart, tail)
│   ├── routes/                # one module per page surface
│   ├── templates/             # Jinja2 templates (base + per-page)
│   └── static/app.css
├── launch_agents/
│   ├── com.personal-agent.relay.plist          # auto-start the relay
│   ├── com.personal-agent.scheduler.plist      # auto-start the scheduler
│   ├── com.personal-agent.log-rotation.plist   # daily at 03:00
│   ├── com.personal-agent.webui.plist            # auto-start the web UI
│   ├── install.sh                              # render + load all four
│   └── uninstall.sh
├── config/
│   ├── personality.md        # editable system-prompt source
│   ├── triggers.yaml         # important sender list + brief schedules
│   └── credentials.json      # Google OAuth client (gitignored)
├── data/                     # gitignored — sqlite dbs, OAuth token, logs
├── pyproject.toml
├── .env.example              # template — copy to .env and fill in
└── README.md
```

---

## Setup

### Quick start (recommended)

For a guided setup that walks through everything — venv + deps + sub-agent selection + API keys + Google OAuth + iMessage relay + LaunchAgents — run:

```bash
cd v1
./install.sh
```

The installer is **idempotent** — re-run anytime to add new sub-agents,
update keys, or reconfigure parts. Existing values are preserved unless
you explicitly change them.

It also handles **migration**: if you point it at another machine's `v1/`
directory at the start, it'll copy `.env`, `config/credentials.json`,
the cached Google OAuth token, and your `data/memory.sqlite` archive
over so you don't lose history when moving servers.

```bash
./install.sh --skip-deps       # reuse existing venv, just re-configure
./install.sh --help
```

The rest of this section explains the same steps manually for anyone
who'd rather do it piece by piece.

### Prerequisites

- Python 3.11+ (any OS for Telegram transport; macOS specifically for iMessage transport)
- `uv` recommended (`brew install uv` on macOS, or via your distro on Linux)
- An always-on host for the relay daemon (Mac or any Linux server for Telegram)
- For **iMessage transport** specifically: macOS (chat.db + AppleScript + sips for HEIC conversion)
- For **Telegram transport**: a bot created via `@BotFather` (5-minute web setup; covered by the installer)

### 1. Install Python dependencies

```bash
cd v1
uv venv --python 3.13
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Or with plain pip:

```bash
cd v1
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure secrets

```bash
cp .env.example .env
```

Edit `.env` and fill in keys. Only `ANTHROPIC_API_KEY` is strictly required — every sub-agent is optional. Pick the ones you want; leave the rest blank and the agent will tell you it doesn't have that capability when asked.

#### Where to get each key

| Env var | Where | Cost | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | Pay-per-token | **Required.** Powers the agent itself. |
| `TODOIST_API_KEY` | [Todoist Settings → Integrations → Developer](https://todoist.com/app/settings/integrations/developer) | Free | Single token. |
| `NOTION_INTEGRATION_TOKEN` | [notion.so/profile/integrations](https://www.notion.so/profile/integrations) → New integration → Internal | Free | **Also share each page/DB with the integration** (page → ⋯ → Connections). Notion permission is opt-in per page. |
| `GITHUB_TOKEN` | [github.com/settings/tokens](https://github.com/settings/tokens) | Free | Classic with `repo` scope, or fine-grained with Issues r+w / PRs r / Contents r / Metadata r. |
| `BRAVE_SEARCH_API_KEY` | [api.search.brave.com](https://api.search.brave.com) → Subscribe Free → API Keys | 2K/mo free | Free tier requires a credit card on file but doesn't charge. |
| `YOUTUBE_API_KEY` | [console.cloud.google.com](https://console.cloud.google.com) → enable "YouTube Data API v3" → Credentials → API key | 10K units/day free | Search costs 100 units; lookups cost 1. SEPARATE from your Google OAuth credentials.json. |
| `DROPBOX_APP_KEY` + `DROPBOX_APP_SECRET` | [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) → Scoped Access app | Free | Permissions: `files.metadata.read`, `files.content.read`, `sharing.read`, optionally `sharing.write`. After setting permissions, copy App key + App secret from Settings, then run `python -m mcp_servers.dropbox_auth` once for browser consent — caches a refresh token so access never expires. |
| `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET` | [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) → Create app | Free (playback needs Premium) | Add `http://127.0.0.1:8765` to the app's Redirect URIs (literal `127.0.0.1`, not `localhost` — Spotify rejects `localhost` since 2025). Then run `python -m mcp_servers.spotify_auth` for browser consent. |

Google OAuth (Gmail + Calendar) is handled separately — see step 3.

### 3. First-time Google auth (Gmail + Calendar)

1. Create a Google Cloud project at [console.cloud.google.com](https://console.cloud.google.com).
2. Enable APIs: **Gmail API** and **Google Calendar API** (APIs & Services → Library).
3. Create OAuth consent screen (External, just for yourself).
4. Credentials → Create credentials → **OAuth client ID** → **Desktop app**.
5. Download the JSON, save as `config/credentials.json` (gitignored).
6. Run the auth flow once:

   ```bash
   python -m mcp_servers.google_auth
   ```

   A browser pops up. Grant Gmail + Calendar permissions. Token cached at `data/google_token.pickle`.

### 4. Configure iMessage relay

In `.env`, choose a mode:

- `IMESSAGE_MODE=self` — text yourself from your iPhone (uses note-to-self chat, requires `TARGET_PHONE_NUMBER` set to your own number plus optional `SELF_HANDLES` for your Apple ID email).
- `IMESSAGE_MODE=contact` — listen to messages from one specific contact (useful for testing or letting someone else use the agent).

Then grant macOS permissions for the daemon to work under launchd:

- **Full Disk Access** for the Python binary (so it can read `~/Library/Messages/chat.db`).
  - System Settings → Privacy & Security → Full Disk Access → `+` → navigate to and add:
    `/opt/homebrew/Cellar/python@3.13/3.13.x/Frameworks/Python.framework/Versions/3.13/Resources/Python.app`
- **Automation → Messages** for AppleScript send (macOS prompts on first send attempt; click Allow).

### 5. Run components

For development, run each daemon in a separate terminal:

```bash
# Interactive REPL (good for testing personality without iMessage)
python agent_host.py

# iMessage relay
python -m relay.imessage_relay --check    # diagnostics
python -m relay.imessage_relay            # daemon

# Scheduler
python -m scheduler.triggers --check                 # diagnostics
python -m scheduler.triggers --run-now morning_brief # fire one trigger now
python -m scheduler.triggers                          # daemon
```

### 6. Auto-start on login (LaunchAgents)

Once everything works manually, install all four launch agents:

```bash
./launch_agents/install.sh
```

This renders absolute paths into the plists, copies them to `~/Library/LaunchAgents/`, and loads them via `launchctl bootstrap`. Four agents:

- `com.personal-agent.relay` — long-running iMessage relay
- `com.personal-agent.scheduler` — long-running scheduler (briefs, reminders, weekly review)
- `com.personal-agent.log-rotation` — daily at 03:00, rotates daemon logs
- `com.personal-agent.webui` — local web admin UI at `http://127.0.0.1:8780`

To remove the LaunchAgents only (keeps the rest of the install intact):

```bash
./launch_agents/uninstall.sh
```

---

## Uninstalling

The interactive uninstaller covers everything from "remove one sub-agent"
to "wipe the whole install." Mirrors the install flow.

```bash
# Interactive menu
python -m tools.uninstall

# Show what's currently installed (sub-agents, LaunchAgents, venv, data)
python -m tools.uninstall --list

# Remove one or several sub-agents (clears env vars + cached tokens)
python -m tools.uninstall --sub-agent dropbox
python -m tools.uninstall --sub-agent canva,linkedin

# Remove LaunchAgents only — stops the daemons, keeps code + data
python -m tools.uninstall --launchagents

# Wipe local data (sqlite, logs, token caches), keep .env + config
python -m tools.uninstall --data

# Full uninstall: LaunchAgents + venv + data + .env + config secrets
python -m tools.uninstall --all                # prompts before destructive steps
python -m tools.uninstall --all --yes          # no prompts (careful)

# Preview without doing anything
python -m tools.uninstall --all --dry-run
```

**What gets removed per sub-agent:**

- Env vars cleared in `.env` (set to empty; comments + structure preserved)
- Cached token file deleted (e.g. `data/dropbox_token.json`)
- The Google family (gmail, calendar, drive, docs, sheets) shares one
  OAuth pickle — the pickle + `config/credentials.json` are only deleted
  when you remove all 5 in one call (`--sub-agent
  gmail,calendar,drive,docs,sheets`)

**What the uninstaller never touches:**

- Source code under `v1/` (delete yourself with `rm -rf v1/` when you're
  truly done)
- Provider-side OAuth authorizations — the local refresh token is gone,
  but the app may still be authorized at the service. The uninstaller
  prints the revocation URL for each sub-agent (e.g. spotify.com/account/apps,
  myaccount.google.com/permissions). Visit each to fully revoke.

---

## Operations

### Semantic recall (memory search)

Past conversations + facts are searchable by meaning, not just literal
substring. Every message and fact is embedded into a 768-dim vector at
archive time using a local `sentence-transformers` model (default
`BAAI/bge-base-en-v1.5`, ~440MB on disk, no API). The agent's
`memory_search_conversations` and `memory_recall_facts` tools score
candidates by cosine similarity, with a small boost for literal
substring matches (best of both for fuzzy and exact queries).

Switch the model via `.env`:

```
EMBEDDER_MODEL=BAAI/bge-base-en-v1.5    # default, balanced
EMBEDDER_MODEL=BAAI/bge-small-en-v1.5   # ~130MB, ~3x faster, modest quality drop
EMBEDDER_MODEL=BAAI/bge-large-en-v1.5   # ~1.3GB, marginal quality bump
```

After changing the model, recompute embeddings:

```bash
sqlite3 data/memory.sqlite "UPDATE messages SET embedding=NULL; UPDATE facts SET embedding=NULL"
python -m tools.backfill_embeddings
```

(The backfill is idempotent; it only touches rows where embedding IS NULL.)

### Inspect what the agent's been doing

```bash
# Recent Anthropic API events
sqlite3 data/memory.sqlite \
  "SELECT timestamp, kind, substr(payload, 1, 80) FROM api_events ORDER BY id DESC LIMIT 20;"

# Cost / token report (last 7 days)
python -m tools.cost_report
python -m tools.cost_report --days 30

# Behavioral analytics (activity by hour/day, tool usage rankings,
# slow turns, conversation lengths)
python -m tools.analytics
python -m tools.analytics --days 30

# Daemon logs
tail -f data/relay.log data/relay.err.log
tail -f data/scheduler.log data/scheduler.err.log
```

### Verify all your API tokens are valid

```bash
python -m tools.token_health
```

Pings each provider's identity / metadata endpoint and reports valid /
invalid / expiring tokens. Catches silent expirations (notably Dropbox
`sl.u.` tokens that auto-expire ~4h after issue) and missing-scope
issues. Each check uses the cheapest free read-only call available;
Brave + YouTube each cost 1 quota unit per run, the rest are free.

Run it weekly, or after rotating any token, or whenever an integration
starts misbehaving.

### Tune the personality

Edit `config/personality.md` and restart the relay (or the agent host if you're running it manually) to pick up changes.

### Add or change scheduled briefs

Edit `config/triggers.yaml` — schedules, important-sender allowlist, urgency keywords. The scheduler re-reads the file on every tick (~30s).

---

## Privacy + secrets

### What's protected

- All API tokens / keys live in `.env` (gitignored — see `.gitignore`).
- The OAuth client JSON lives in `config/credentials.json` (gitignored).
- The cached OAuth token lives in `data/google_token.pickle` (gitignored).
- All SQLite DBs and daemon logs live in `data/` (gitignored).

The only files committed to git are source code, configs (`personality.md`, `triggers.yaml`), the `.env.example` template (no real values), and the launchd plists (which use `__V1_DIR__` placeholders — actual paths are filled in only on the local machine during install).

The agent talks to Anthropic's API; Anthropic's commercial privacy terms apply (no training on API data, 30-day retention by default). Every API event is also logged locally in `data/memory.sqlite` (`api_events` table) so you can audit independently.

### Key rotation + handling discipline

Treat every key in `.env` like a password. **A key is compromised the moment it leaves the file.**

**Never paste keys into:**
- Chat tools (Slack, iMessage, this assistant's transcript, GitHub Copilot Chat, anything with a server-side history)
- Issue trackers, PRs, comments
- Screenshots or screen recordings
- Documentation pulled into Drive / Notion / Confluence
- Cloud-synced text files (Notes, Obsidian-with-sync, etc.)
- Browser address bar / bookmarks (URLs leak via history sync)

**If a key is exposed (intentionally during setup or accidentally), rotate it.** Don't hope nobody noticed.

**Rotation cadence and procedure for each:**

| Token | Where to revoke + regenerate | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys → revoke, create new | If revoked, the relay will start erroring; update `.env` and reload (`./launch_agents/install.sh` re-bootstraps). |
| `TODOIST_API_KEY` | [Todoist Settings → Integrations → Developer](https://todoist.com/app/settings/integrations/developer) → "Reset" | Single user-level token. |
| `NOTION_INTEGRATION_TOKEN` | [notion.so/profile/integrations](https://www.notion.so/profile/integrations) → your integration → Configuration → "Generate new token" | Old token is invalidated. Page-share connections persist. |
| `GITHUB_TOKEN` | [github.com/settings/tokens](https://github.com/settings/tokens) → "Delete" / "Regenerate" | If you used a fine-grained token, the new one inherits the same scopes if you regenerate. |
| `BRAVE_SEARCH_API_KEY` | api.search.brave.com → API Keys → delete + create new | |
| `YOUTUBE_API_KEY` | console.cloud.google.com → APIs & Services → Credentials → delete + create new | While you're there, restrict the new one to "YouTube Data API v3" only. |
| `DROPBOX_ACCESS_TOKEN` | [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) → your app → Settings → Generate new access token | Short-lived `sl.u.` tokens auto-expire in ~4h regardless. |
| Google OAuth (Gmail+Calendar) | console.cloud.google.com → APIs & Services → Credentials → delete the OAuth client; OR revoke the token at myaccount.google.com → Security → Third-party access | Then re-download `credentials.json`, delete `data/google_token.pickle`, re-run `python -m mcp_servers.google_auth`. |

**Recommended schedule:**
- Rotate every key at minimum once per quarter, even with no known exposure.
- Rotate immediately after: sharing a key with a build assistant; granting someone else temporary access to your machine; a laptop loss/theft event; a data-breach announcement at any of the providers.

**Detection:** the cost report (`python -m tools.cost_report --days 7`) shows daily Anthropic spend. Spikes can indicate either heavy use or someone else using your key. Each provider's web console also has usage/billing dashboards.

### When sharing setup with another assistant

If you ask a build/coding assistant (Claude, ChatGPT, Cursor, etc.) to help configure or debug this project:

- **Don't paste real tokens.** Share placeholders (`<my-anthropic-key>`) and let the assistant guide you to put real values into `.env` yourself.
- If you DO paste a real key (the assistant needs it to live-test), **rotate that key as soon as the session ends**.
- Skim assistant transcripts before saving them to Drive / sharing with teammates — they can contain pasted secrets.

The local-only files this project produces (`.env`, `config/credentials.json`, `data/`) are the trust boundary. Anything that leaves those files via copy/paste/screenshot has effectively been published — assume so and rotate.

---

## What's not in v1

See **[ROADMAP.md](./ROADMAP.md)** for the full planned list — what each
item adds, why it's not in yet, and what's needed to land it. The roadmap
distinguishes items that are remote-buildable today from items that need
local Mac access (browser-OAuth flows, device-bound account setup, etc.).

Highlights:

- **Remote-buildable now:** group chat support in the relay, Discord /
  Slack / SMS transports, LLM-classified email watch.
- **Needs local Mac:** Calendar writes, Drive / Docs / Sheets, Dropbox
  OAuth refresh flow, Spotify, Canva, LinkedIn, dedicated agent identity.
- **Operational improvements:** tighter brief/review prompts, audit-log
  analytics, "query archive" SQL tool, recurring reminders.
