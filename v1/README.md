# Personal Agent — v1

A personal AI agent that runs as a daemon on your Mac, talks to you over **iMessage**, **Telegram**, **Discord**, or **Slack** (your choice at install time), and helps with email, tasks, calendar, files, sleep tracking, music, search, and more. There's also a local web UI at `http://127.0.0.1:8780`.

This is a **personal project**, shared publicly as-is so others can fork it for their own setup or copy ideas out of it. It's not a product, not a hosted service, and not actively marketed. If something doesn't fit your needs, fork it and change it — that's the intended use.

## What this is / what it isn't

**What this is:**

- A single-user, local-first AI agent. Runs as four LaunchAgents on your Mac. Talks to one Anthropic API key under one user's control.
- A Claude reasoning loop with 27 in-process MCP sub-agents wired up to apps you probably already use (Gmail, Calendar, Drive, Todoist, GitHub, Notion, Spotify, Apple Reminders/Notes/Music/Mail/Photos, Eight Sleep, Google/OpenStreetMap, Brave Search, etc.).
- An iMessage/Telegram/Discord/Slack relay so you can text the agent from anywhere.
- A scheduler that fires morning briefs, a Sunday weekly review, and email/delivery alerts.
- A local-only web admin UI for configuration, observability, and a browser chat surface.

**What this isn't:**

- A hosted SaaS. There's no cloud, no signup, no shared infrastructure. You bring your own API keys and run it on your own machine.
- A multi-tenant agent framework. There's a single allowlisted user; the safety model assumes that user owns the machine.
- Production-grade. Maintained on the side as a personal tool. APIs and configs change between commits without deprecation notices.
- Provider-agnostic. The agent uses Claude (via the Claude Agent SDK) end-to-end. Other LLM providers are tracked on the roadmap but not implemented.

This directory (`v1/`) holds the implementation. The historical design spec is in `../pre_requirements.md` (kept for reference; many decisions diverge from it). License is MIT; see `../LICENSE`.

## Costs

Every Claude turn costs real money. This isn't a free demo. Typical operating cost on my own daily use:

- **Conversational replies (Sonnet 4.6):** ~$0.01–0.05 per turn depending on context size.
- **Scheduled briefs (Opus 4.7):** ~$0.10–0.30 per fire, twice a day (morning brief + Sunday review).
- **Email/vision/classifier calls (Haiku 4.5):** sub-cent each.

A typical day at moderate use lands around **$1–3** in Anthropic spend. Heavy build days (re-running scheduler triggers while iterating on personality) can spike to $10+.

You watch the spend locally with:

```bash
python -m tools.cost_report          # last 7 days
python -m tools.cost_report --days 30
```

The web UI's Observability page shows the same data.

Provider-side billing dashboards (Anthropic console, Google Cloud, etc.) are the source of truth — the local cost report is a convenience.

## Privacy & security profile

> ⚠ Read this before you configure anything.

This is a **single-user, local-first** tool. It stores a lot of
personal data on your machine and sends a lot of personal data to
Anthropic on your behalf. Understand the profile before running it.

### What lives on your local machine

| Path | What's in it |
|---|---|
| `data/memory.sqlite` | every conversation with the agent, every fact extracted about you, and a verbatim audit log of every Claude API payload (see the `api_events` table). Plaintext SQLite today. |
| `.env` | API keys for every sub-agent you've enabled. For Eight Sleep specifically: a real account password. Plaintext. |
| `config/credentials.json` | Google OAuth client secret. Plaintext. |
| `data/*_token.json`, `data/google_token.pickle` | live OAuth refresh tokens for Gmail, Calendar, Drive, Spotify, Dropbox, etc. A stolen token gives a third party **indefinite** access to that service until you revoke it at the provider. |
| `data/*.log` | daemon logs with first-80-char previews of incoming messages and email-triage decisions. |
| `data/uploads/` | every image you've attached in the web chat. |

These files are gitignored by default. Keep the entire `v1/` folder
**out of cloud sync** (iCloud Drive, Dropbox, Google Drive), **off
shared NFS mounts**, and **excluded from Time Machine backups**
unless you fully understand the implications.

### What gets sent over the network

- **Anthropic** — every message you send the agent, every assistant
  reply, every tool call, AND every email body the scheduler triages
  (capped at 4000 chars per email). Anthropic's commercial terms apply
  here: no training on your data, 30-day retention by default. You can
  audit what was sent locally by reading the `api_events` table in
  `data/memory.sqlite`.
- **Each sub-agent's provider** — Gmail / Calendar / Drive talk to
  Google. Telegram / Discord / Slack talk to their respective
  platforms. Spotify, Dropbox, Eight Sleep, Brave Search, Open-Meteo,
  YouTube, Notion, GitHub, Todoist, Google Maps / OpenStreetMap each
  talk to their own API. Sub-agents you haven't enabled never talk to
  anyone.
- **No analytics, no telemetry, no crash reporting.** Nothing else
  leaves the machine.

### Threat models this protects against

- **Honest mistakes.** A PreToolUse hook blocks any attempt to auto-
  send email; the agent only writes Drafts (Gmail and Apple Mail).
- **Casual local-network attackers.** The web UI binds to `127.0.0.1`
  only — never exposed on the LAN.
- **Public-repo accidental leaks.** A strong `.gitignore` excludes
  `.env*`, `config/credentials.json`, `config/triggers.yaml`,
  `data/*.sqlite*`, `data/*.log`, and every `data/*_token.json` /
  `data/*.json` / `data/*.pickle`.

### Threat models this does NOT protect against

- **A stolen or shared laptop where someone has your OS account.**
  All files under `data/` and `.env` are readable by the OS user that
  runs the daemons. Today they sit at `0o644` (world-readable on your
  machine — fix is on the roadmap). Use FileVault and don't share
  user accounts.
- **A malicious local app running under your OS user.** It has the
  same filesystem access you do, and can read `.env` + token caches.
- **A malicious website visited in another browser tab while the web
  UI is running.** State-changing POSTs to `/chat`, `/config/env`,
  and `/settings/connect/...` don't have CSRF protection in v1. The
  hard `127.0.0.1` binding is the only line of defense; treat the
  web UI as you would `localhost:5432` for a database.
- **Privacy of third parties in group chats you opt in.** When you
  set `IMESSAGE_GROUP_CHATS`, the agent reads and archives messages
  from everyone in those threads — not just you. Tell them, or don't
  opt that group in.
- **Backups outside the project folder.** Time Machine, iCloud Drive,
  and similar will happily back up `v1/data/` to a separate trust
  domain unless you exclude it.

### What's planned to harden this

See the [Security enhancements](./ROADMAP.md#security-enhancements)
section in ROADMAP.md.

**Shipped in batch 1 (2026-05-14):**
- **H1** — tokens / DB / logs now write at `0o600`, with
  `tools/repair_permissions.py` available to one-shot fix anything
  on disk from earlier runs (`python -m tools.repair_permissions`).
- **H3** — the web UI ignores `WEB_HOST` overrides unless you also
  set `WEB_ALLOW_LAN=1`. A typo can't accidentally expose it.
- **M1** — the `/config/env` editor now masks PII (phone, address,
  account IDs) the same way it masks credentials, with a per-row
  reveal button.
- **M2** — daemon log previews trimmed from 80 → 20 chars.
  `tools/rotate_logs.py` already prunes anything older than 7 days.

**Shipped in batch 2 (2026-05-14):**
- **H5** — Eight Sleep password lives in the macOS Keychain. Run
  `python -m tools.eightsleep_set_password` to migrate. `.env`
  password fallback still works (for Linux/Windows forks) with a
  deprecation reminder at startup.
- **M4** — set `EMAIL_TRIAGE_LOCAL_ONLY=true` in `.env` to stop
  the scheduler from sending any email content to Anthropic. The
  morning brief gains a "📧 triaged N email(s) to Anthropic in the
  last 24h" visibility line so the data flow is in your face.
- **M5** — image uploads in the web chat get purged when the
  conversation closes, and the entire `data/uploads/` tree is
  size-capped via `UPLOADS_TOTAL_CAP_MB` (default 500 MB) with
  oldest-first cleanup once the cap is exceeded.

**Shipped in batch 3 (2026-05-14):**
- **H2** — `api_events` (the verbatim Claude API audit log) is now
  purged daily by the scheduler once rows are older than
  `audit_log.audit_log_retention_days` (default 30, configured in
  `triggers.yaml`). Conversations / messages / facts / reminders are
  never touched — only the audit-log table. SQLCipher whole-DB
  encryption was the preferred fix but doesn't have arm64-macOS-
  Python-3.13 wheels yet; the retention purge ships in its place.
- **M3** — group-chat messages from other people now get tagged
  `is_third_party=1` at archive time and purged after
  `group_chat.group_chat_retention_days` (default 30). The web UI's
  history page hides third-party rows by default with a "show them"
  toggle; when shown they get an amber "group member" label so
  they're visually distinct from your own content.

**Shipped in batch 4 (2026-05-14):**
- **H4** — `git filter-repo` ran in 4 passes (1 path removal + 2
  blob-callback content replacements + 1 message-callback for a
  commit-message body). The original 15-email allowlist AND every
  residual personal-name reference in tracked files / commit
  messages are now genericized to neutral placeholders. Final grep
  over `git log --all -p` returns 0 matches across all 18 original
  tokens. Pre-rewrite repo backup saved at
  `~/personal_agent_backup_before_H4_<timestamp>.tgz` (283 MB).
  Force-push to GitHub is deferred until the user is ready; the
  local repo is in the rewritten state and won't match a pre-
  existing remote history graph.

**Still active in the security section:** none. Every H/M item is
shipped. The L-tier items remain "recorded for future, no
implementation planned."

The deeper "key rotation + handling discipline" guidance lives in the
[Privacy + secrets](#privacy--secrets) section further down.

## Choosing a transport

Set `RELAY_TRANSPORT` in `.env` to one of:

- **`imessage`** — macOS-only. Polls `~/Library/Messages/chat.db` and sends via AppleScript. Requires Full Disk Access + Automation permissions for the daemon. Native iPhone integration; the agent appears as a "Note to Self" thread (or a regular contact in `contact` mode).
- **`telegram`** — Cross-platform. The agent runs as a bot you create via `@BotFather`; only allowlisted Telegram user IDs can talk to it. Works from any Mac, Linux, or Windows host with Python — no iMessage / chat.db dependency, and no iOS Focus / DND quirks.
- **`discord`** — DM + opt-in server channel support via a bot you create in the Discord Developer Portal. Allowlisted by Discord user ID for DMs; allowlisted by channel ID for server channels (with @-mention or trigger-phrase gating). Image attachments route through the vision sub-agent.
- **`slack`** — Socket Mode app (no public URL needed). DM + opt-in channel / group / mpim support, allowlisted by Slack user ID and channel ID. Image attachments via vision.
- **`sms`** — Text-only via Twilio. Universal reach (any phone, any carrier) but no image attachments. Bidirectional via a Twilio webhook the relay hosts at `127.0.0.1:8781/sms/webhook`; needs a public URL (ngrok for dev, reverse proxy for prod) for Twilio to deliver inbound messages. ~$1/mo for the phone number + ~$0.008 per message.

Switch transports any time by editing `.env` and restarting the relay (`launchctl kickstart -k gui/$(id -u)/com.personal-agent.relay`). Only one transport runs at a time per relay process. The interactive installer (`./install.sh`) walks you through choosing one.

### Group chats (iMessage, Telegram, Discord, Slack)

All four chat transports can additionally listen in **group chats / server channels** so the agent can be summoned in family, work, or club threads — not just 1:1. Group support is opt-in and additive: it runs alongside the primary DM mode and never displaces it.

- **iMessage:** set `IMESSAGE_GROUP_CHATS` to a comma-separated list of either `chat_identifier` values (like `chat657054710918744555`) or `display_name` values (like `Family`). Run `python -m relay.imessage_relay --check` to print every group visible in your `chat.db` so you can copy the right value. Trigger phrases default to `@agent, hey agent, agent,` — override with `IMESSAGE_GROUP_TRIGGERS`.
- **Telegram:** add the bot to the group, then optionally set `TELEGRAM_ALLOWED_CHAT_IDS` to restrict which groups it'll respond in. By default it accepts `@<bot_username>` mentions plus the same fallback triggers — override with `TELEGRAM_GROUP_TRIGGERS`. Telegram bots default to "privacy mode" and only see direct mentions in groups; flip via `@BotFather` → `/setprivacy` → Disable to let the bot see all messages.
- **Discord:** set `DISCORD_ALLOWED_CHANNEL_IDS` to a comma-separated list of channel IDs (Developer Mode → right-click channel → Copy Channel ID). The bot listens in those channels when a message contains its `<@bot_id>` mention or matches `DISCORD_GROUP_TRIGGERS`.
- **Slack:** set `SLACK_ALLOWED_CHANNEL_IDS` to a list of channel IDs (Cxxxxx for public channels, Gxxxxx for private). You ALSO have to add `message.channels` / `message.groups` / `message.mpim` to the app's Event Subscriptions in the Slack app config — otherwise the bot can't see channel messages. Triggers gated by `SLACK_GROUP_TRIGGERS` or an explicit `<@bot_user_id>` mention.

In group mode the agent only responds when a trigger matches, replies in-thread, and follows tighter etiquette (no private inbox contents, terser replies) defined in `config/personality.md`. Scheduled briefs / reminders still go to the primary 1:1 destination — they never land in a group. Third-party messages (from other group members) are tagged in the archive and purged after `group_chat_retention_days` (default 30; configurable in `triggers.yaml`).

---

## Capabilities at a glance

The agent is a single Claude reasoning loop with 27 in-process MCP sub-agents. You text it, it picks the right tools.

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
| **vision** | Describe attached images (HEIC auto-converted) | Anthropic API | metered |
| **web** | Brave Search + URL fetch | API key | ✅ 2K/mo free |
| **youtube** | Search + video/channel metadata | API key | ✅ 10K units/day |
| **dropbox** | Search, list, read text, share-link (OAuth refresh flow) | OAuth refresh | ✅ free |
| **spotify** | Search, playback, queue, playlists, devices | OAuth refresh | ✅ free (playback needs Premium) |
| **wikipedia** | Search, summary, full article extract | None needed | ✅ free |
| **reddit** | Subreddit top/hot, search, post + comments | None (public read) | ✅ free |
| **reminders** | Schedule "remind me at 4pm to..." | None needed | ✅ free |
| **reminders_apple** | Apple Reminders.app — list / create / complete / delete | AppleScript (macOS) | ✅ free |
| **notes_apple** | Apple Notes.app — list / search / read / append / create | AppleScript (macOS) | ✅ free |
| **photos_apple** | Apple Photos.app — albums + date-range search (read-only) | AppleScript (macOS) | ✅ free |
| **music_apple** | Apple Music.app — playback control + library search | AppleScript (macOS) | ✅ free |
| **mail_apple** | Apple Mail.app — read + draft (never sends) | AppleScript (macOS) | ✅ free |
| **maps** | search_places / drive_time / geocode / reverse_geocode | Google Maps key, or free OSM | varies |
| **eightsleep** | Last-night sleep summary + bed temp control | Email/password | ✅ free (req. Eight Sleep sub) |

Plus: **scheduled morning brief** (~7:30 AM) and **Sunday weekly review** (8 PM) auto-pushed to your active transport.

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
- **Facts + Reminders** — list + create + deactivate / cancel directly from the browser
- **Settings** — sub-agent status dashboard with one-click Connect buttons (subprocess + SSE streams the OAuth script's stdout live) and a "install / reload LaunchAgents" button
- **Transports** — guided picker at `/settings/transports` with radio cards for the 5 transports, per-transport field walkthroughs (labels + inline help + secret masking + reveal toggles), a "verify" button that runs the matching `--check` and SSE-streams the output, and an in-place save that writes to `.env` while preserving comments
- **Install wizard** — `/install` detects a fresh checkout (no `.env` / empty `ANTHROPIC_API_KEY`), bootstraps `.env` from `.env.example`, and walks you into the settings page with a first-run banner
- **Chat image attachments** — drag-and-drop or 📎 picker; images saved under `data/uploads/<conv_id>/` and routed through the vision sub-agent same as the iMessage / Telegram / Discord / Slack relays

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
you explicitly change them. The relay step prompts for group-chat
allowlists (`IMESSAGE_GROUP_CHATS` / `TELEGRAM_ALLOWED_CHAT_IDS`) and
trigger phrases on top of the primary 1:1 mode.

The web UI's `.env` editor at `/config/env` surfaces any variables
present in `.env.example` but missing from your live `.env` in an
"Available in .env.example" block at the top — so when the template
adds new vars (like the group-chat ones), you can fill them in
through the browser without re-running the installer.

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
