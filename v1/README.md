# Personal Agent — v1

A personal AI agent that runs locally on your Mac, talks to you over iMessage, and helps with email triage, task management, and calendar awareness.

This directory holds the v1 MVP implementation. The historical design spec is in `../pre_requirements.md` (kept for reference; many decisions diverge from it).

## What v1 does

- **Reads Gmail** — search, read, archive, draft replies. **Never sends.** Drafts go to Gmail Drafts; you send manually from the Gmail UI.
- **Manages Todoist** — list/create/update/complete tasks across projects, daily and overdue filters.
- **Reads Google Calendar** — list events, search, check availability. (Event creation deferred to v2.)
- **Remembers** — every conversation is archived locally. A lightweight extraction pass distills facts/preferences and injects them into the agent's system prompt for context across sessions.
- **Sends scheduled briefings** — morning brief between 7–9 AM, weekly review Sunday evening.
- **Sends proactive nudges** — pings you over iMessage when an email arrives from an important sender or matches an urgency keyword.

## What v1 does *not* do

- Send email (drafts only, by design — see *Safety contract* below)
- Auto-archive or auto-act on incoming email/tasks without your explicit say-so
- Calendar event creation/update/delete
- Docs/Sheets/Drive/Notion/Dropbox/YouTube/LinkedIn/Canva/Reddit/GitHub
- Image generation, file analysis, web search, voice notes
- Vector-search memory (uses simple SQLite + extracted facts only)

These are deferred until v1 is stable.

## Safety contract

Three hard rules, enforced in three places (system prompt + tool surface + SDK pre-tool hook):

1. **Never auto-send email.** No `send_email` tool exists. Drafts go to Gmail Drafts. The user is the only one who hits send.
2. **Never modify shared external state without confirmation.** Reading is free; writing requires explicit user direction in the conversation.
3. **All Claude API traffic is logged locally** (`data/memory.sqlite`, table `api_events`) so you can audit what the agent sent to Anthropic.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Mac (LaunchAgents auto-start everything below)          │
│                                                          │
│  ┌──────────────────────┐  ┌─────────────────────────┐   │
│  │  iMessage relay      │  │  Scheduler              │   │
│  │  (polls Messages.app │  │  (cron: 7am brief,      │   │
│  │   ~5s, calls agent)  │  │   Sun 8pm review)       │   │
│  └──────────┬───────────┘  └────────────┬────────────┘   │
│             │ async invoke              │                │
│             ▼                           ▼                │
│  ┌──────────────────────────────────────────────────┐    │
│  │       Agent host (Python, Claude Agent SDK)      │    │
│  │   • single Claude reasoning loop                 │    │
│  │   • personality system prompt (lowercase peer)   │    │
│  │   • injects memory facts via context builder     │    │
│  │   • SDK hooks enforce safety invariants          │    │
│  └────┬───────────┬──────────┬───────────┬──────────┘    │
│       │           │          │           │               │
│   ┌───▼───┐ ┌─────▼───┐ ┌────▼───┐ ┌────▼─────────┐      │
│   │Gmail  │ │Todoist  │ │Calendar│ │Memory MCP    │      │
│   │ MCP   │ │ MCP     │ │  MCP   │ │(archive +    │      │
│   │(read, │ │(CRUD,   │ │(read-  │ │ facts +      │      │
│   │ draft)│ │ filter) │ │ only)  │ │ audit log)   │      │
│   └───────┘ └─────────┘ └────────┘ └──────────────┘      │
│       │         │          │            │                │
│   Gmail API  Todoist   Calendar    Local SQLite          │
│   (OAuth)    REST API   API                              │
└──────────────────────────────────────────────────────────┘
```

## Layout

```
v1/
├── agent_host.py          # Main entry: Claude Agent SDK loop
├── system_prompt.py       # Personality + memory injection
├── relay/
│   └── imessage_relay.py  # Polls Messages.app, sends back
├── scheduler/
│   └── triggers.py        # Morning brief + Sunday review
├── mcp_servers/
│   ├── gmail_server.py    # read, search, draft, archive (NO send)
│   ├── todoist_server.py  # CRUD, daily/overdue filters
│   ├── calendar_server.py # list, search, availability (read-only)
│   └── memory_server.py   # archive + fact extraction + recall
├── memory/
│   └── store.py           # SQLite layer: archive + audit + facts
├── config/
│   ├── personality.md     # editable system-prompt source
│   └── triggers.yaml      # important sender list + keywords
├── data/                  # gitignored — memory.sqlite, OAuth token cache
├── pyproject.toml
├── .env.example
└── README.md
```

## Setup

Prerequisites already in place (per build plan):
- Anthropic API key
- Google Cloud project with Gmail + Calendar APIs enabled, OAuth client credentials downloaded
- Todoist API key
- Always-on Mac that won't sleep

### 1. Install Python deps

Recommended via [uv](https://docs.astral.sh/uv/) (fast):

```bash
cd v1
uv venv
uv pip install -e ".[dev]"
```

Or plain pip:

```bash
cd v1
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure secrets

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY, TODOIST_API_KEY, USER_TIMEZONE, TARGET_PHONE_NUMBER
```

Place your Google OAuth credentials JSON at `config/credentials.json` (gitignored).

### 3. First-time Google auth

```bash
python -m mcp_servers.gmail_server --auth
```

This pops a browser, you grant access, the token is cached at `data/google_token.pickle`.

### 4. Run components

Each component is its own process. In dev, run them in separate terminals; in production, install as LaunchAgents.

```bash
# Terminal 1 — agent host
python agent_host.py

# Terminal 2 — iMessage relay (Mac only)
python -m relay.imessage_relay

# Terminal 3 — trigger scheduler
python -m scheduler.triggers
```

## Build status

Step 1 of 5 (skeleton) — **in progress**. See the build plan in conversation history.

| Step | Status |
|---|---|
| 1. Scaffold v1/ skeleton + README | done |
| 2. Stand up agent host with personality (no integrations yet) | done |
| 3. Memory MCP server (audit log + conversation archive + facts) | done |
| 4. Gmail / Todoist / Calendar MCP servers | done |
| 5a. iMessage relay (contact + self mode) | done |
| 5b. Scheduler (morning brief + Sunday review) | not started |
| 5c. LaunchAgent plists | not started |

## Personality

The agent's tone is defined in `config/personality.md` and loaded at startup. Edit that file to retune; the change takes effect on next agent host restart.

## Privacy

The agent talks to Anthropic's API. All traffic (user inputs, assistant text, tool calls, tool results, end-of-turn metadata) is logged to `data/memory.sqlite` in the `api_events` table so you can audit what's been sent. Anthropic's commercial privacy terms apply by default — no training on API data, 30-day retention.

To inspect:

```bash
sqlite3 data/memory.sqlite "SELECT timestamp, kind, substr(payload, 1, 80) FROM api_events ORDER BY id DESC LIMIT 20;"
```
