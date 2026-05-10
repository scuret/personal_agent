# Personal Agent — v1

A personal AI agent that runs locally on your Mac, talks to you over iMessage, and helps with email, tasks, calendar, files, search, and more.

This directory holds the v1 implementation. The historical design spec is in `../pre_requirements.md` (kept for reference; many decisions diverge from it).

---

## Capabilities at a glance

The agent is a single Claude reasoning loop with twelve in-process MCP sub-agents. You text it, it picks the right tools.

| Sub-agent | What it does | Auth | Free? |
|---|---|---|---|
| **memory** | Conversation archive + extracted facts + audit log | Local SQLite | ✅ free |
| **gmail** | Search, read, draft, archive. **Never sends.** | Google OAuth | ✅ free |
| **calendar** | Read events, search, free/busy check | Google OAuth | ✅ free |
| **todoist** | List/create/update/complete tasks | API token | ✅ free |
| **notion** | Search, read, query DBs, create page, append | Integration token | ✅ free |
| **github** | Repos, issues, PRs, commits, search, create issue | PAT | ✅ free |
| **weather** | Current + N-day forecast (Open-Meteo) | None needed | ✅ free |
| **vision** | Describe iPhone-attached images (HEIC auto-converted) | Anthropic API | metered |
| **web** | Brave Search + URL fetch | API key | ✅ 2K/mo free |
| **youtube** | Search + video/channel metadata | API key | ✅ 10K units/day |
| **dropbox** | Search, list, read text, share-link | Access token | ✅ free |
| **wikipedia** | Search, summary, full article extract | None needed | ✅ free |
| **reddit** | Subreddit top/hot, search, post + comments | None (public read) | ✅ free |
| **reminders** | Schedule "remind me at 4pm to..." | None needed | ✅ free |

Plus: **scheduled morning brief** (~7:30 AM) and **Sunday weekly review** (8 PM) auto-pushed to iMessage.

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
├── launch_agents/
│   ├── com.personal-agent.relay.plist          # auto-start the relay
│   ├── com.personal-agent.scheduler.plist      # auto-start the scheduler
│   ├── com.personal-agent.log-rotation.plist   # daily at 03:00
│   ├── install.sh                              # render + load all three
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

### Prerequisites

- macOS (for chat.db + AppleScript + sips)
- Python 3.11+
- `uv` recommended (`brew install uv`)
- An always-on Mac (won't sleep) for the relay daemon

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
| `DROPBOX_ACCESS_TOKEN` | [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) → Scoped Access app | Free | Permissions: `files.metadata.read`, `files.content.read`, `sharing.read`, optionally `sharing.write`. **Click Submit, then regenerate token after permission changes.** |

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

Once everything works manually, install all three launch agents:

```bash
./launch_agents/install.sh
```

This renders absolute paths into the plists, copies them to `~/Library/LaunchAgents/`, and loads them via `launchctl bootstrap`. Three agents:

- `com.personal-agent.relay` — long-running iMessage relay
- `com.personal-agent.scheduler` — long-running scheduler (briefs, reminders, weekly review)
- `com.personal-agent.log-rotation` — daily at 03:00, rotates daemon logs

To remove:

```bash
./launch_agents/uninstall.sh
```

---

## Operations

### Inspect what the agent's been doing

```bash
# Recent Anthropic API events
sqlite3 data/memory.sqlite \
  "SELECT timestamp, kind, substr(payload, 1, 80) FROM api_events ORDER BY id DESC LIMIT 20;"

# Cost / token report (last 7 days)
python -m tools.cost_report
python -m tools.cost_report --days 30

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

- **Remote-buildable now:** Pushover backup channel, vector memory (Voyage),
  stocks/crypto, Wikipedia, Reddit (public read), group chat support in
  the relay.
- **Needs local Mac:** Calendar writes, Drive / Docs / Sheets, Dropbox
  OAuth refresh flow, Spotify, Canva, LinkedIn, dedicated agent identity.
- **Operational improvements:** tighter brief/review prompts, audit-log
  analytics, "query archive" SQL tool, recurring reminders.
