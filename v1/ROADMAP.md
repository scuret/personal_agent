# Roadmap

What's shipped, what's planned, and what each planned item needs to actually land.

## Shipped

15 sub-agents currently live: **memory, archive (aggregate analytics), todoist, gmail, calendar (read), weather, vision, notion, github, web (Brave search + URL fetch), youtube, dropbox, wikipedia, reddit (public read), reminders.**

Plus operational tooling and infrastructure:
- iMessage relay (contact + self mode, attributedBody decoder for DND-suppressed messages)
- Telegram relay (alternative transport, allowlisted user IDs, image-attachment support)
- Pluggable transport via `RELAY_TRANSPORT` + `relay/run.py` dispatcher
- Recurring reminders (daily / weekdays / weekly / monthly)
- Rules-based email-watch trigger (sender allowlist + urgency keywords, polled every N minutes)
- Morning brief + Sunday weekly review scheduler with wallclock-based catchup (survives Mac sleep)
- Cost dashboard (`tools/cost_report.py`)
- Behavioral analytics (`tools/analytics.py`)
- Token health check (`tools/token_health.py`)
- Daily log rotation (`tools/rotate_logs.py` + LaunchAgent)
- Interactive installer (`./install.sh` → `tools/install.py`) with sub-agent selection, key prompts, Google OAuth flow, transport choice, triggers config, LaunchAgent install, and migration from another machine
- LaunchAgent auto-start for relay + scheduler + log-rotation
- Audit log of every Anthropic API event (privacy invariant)
- Dynamic sub-agent registration (only configured ones load)
- Send-block PreToolUse hook + isolation knobs (`tools=[]` + `strict_mcp_config=True`)
- Key-rotation + handling-discipline guidance (README + .env.example banner)

## Planned

Each item lists what it adds, why it's not in yet, and what unblocks it.

### Pushover — backup push channel
- **What:** Secondary delivery path that bypasses iOS Focus / DND for the morning brief, weekly review, urgent triggers, and reminders. iMessage stays the primary channel; Pushover is the safety net.
- **Why deferred:** Just hadn't gotten to it yet.
- **Unblocks:** Web signup at pushover.net for a user key + an app token. Both go in `.env`. **Remote-buildable.**
- **Effort:** ~30 min.

### Vector memory (Voyage AI embeddings)
- **What:** Replace SQLite `LIKE` substring search in `memory_search_conversations` with semantic search over the conversation archive. Lets the agent find "that thing we discussed about wedding planning" without exact-keyword matches.
- **Why deferred:** Wanted to settle daily-use patterns first. Current substring search is adequate for short-term recall.
- **Unblocks:** Voyage AI signup at voyageai.com → API key in `.env`. Schema change to add an `embeddings` column to `messages`, plus a backfill script for existing messages, plus inline embedding on each archived turn.
- **Cost:** Voyage's `voyage-3.5-lite` is $0.02 per million tokens — backfilling 200K tokens costs <$0.01.
- **Remote-buildable.**
- **Effort:** ~hour.

### Stocks / crypto
- **What:** Sub-agent that returns price quotes, recent performance, basic fundamentals. "What's BTC at?" / "Show me NVDA's last 30 days."
- **Why deferred:** Not surfaced in real usage yet.
- **Unblocks:** Pick a provider — CoinGecko (no auth, crypto only) and/or Alpha Vantage (free key, stocks). Code only after that's chosen.
- **Remote-buildable** (CoinGecko needs no signup; Alpha Vantage takes ~2 min on the web).
- **Effort:** ~45 min.

### ~~Wikipedia~~ — shipped
### ~~Reddit (public read-only)~~ — shipped

### Calendar writes (create / update / delete events)
- **What:** Extend the existing calendar MCP server with the three write tools we already coded but had to revert.
- **Why deferred:** Requires re-running the Google OAuth consent flow with `calendar.events` scope (currently only `calendar.readonly`).
- **Unblocks:** Re-auth at the Mac. The code is in commit history (`905b80c`'s parent — actually it was reverted before commit; needs to be re-implemented per the deferred plan). Run `python -m mcp_servers.google_auth` after updating `SCOPES`.
- **NOT remote-buildable** (needs browser).
- **Effort:** ~30 min once at the Mac.

### Drive / Docs / Sheets
- **What:** Read+write Google Drive (search/list/move), Docs (read/append/replace), Sheets (read/write/append rows).
- **Why deferred:** Same OAuth re-auth requirement as Calendar writes — needs `drive.file` or broader Drive scopes plus `documents` and `spreadsheets`.
- **Unblocks:** OAuth scope expansion at the Mac.
- **NOT remote-buildable.**
- **Effort:** ~half-day for all three sub-agents.

### Dropbox OAuth refresh flow
- **What:** Replace short-lived `sl.u.` access tokens (4h expiration) with the OAuth refresh-token flow so Dropbox keeps working indefinitely without re-pasting tokens.
- **Why deferred:** Browser-based authorization required for the initial consent.
- **Unblocks:** Implement the refresh flow in `mcp_servers/google_auth.py`-style module specifically for Dropbox; do the consent at the Mac once.
- **NOT remote-buildable.**
- **Effort:** ~hour.

### Spotify
- **What:** Search / play / queue / library / playlist tools. Read access is the most useful piece for a personal agent.
- **Why deferred:** OAuth flow with browser consent.
- **Unblocks:** Spotify Developer app registration (web), then a one-time browser OAuth at the Mac.
- **NOT remote-buildable.**
- **Effort:** ~hour.

### Canva
- **What:** Create / search / export designs, list folders.
- **Why deferred:** OAuth-based Connect API, browser consent required. Also lower priority than the productivity-focused integrations.
- **Unblocks:** Canva developer registration + OAuth flow at the Mac.
- **NOT remote-buildable.**
- **Effort:** ~hour to wire core tools (the source spec mentions "53+ tools" but we'd start with create/search/add-text/export and grow).

### LinkedIn
- **What:** Profile read, post search, post creation.
- **Why deferred:** OAuth-based, AND most useful endpoints are restricted to Marketing/Talent partner apps. A personal LinkedIn integration token gets only profile + minimal post info — limited utility.
- **Unblocks:** App registration + OAuth at the Mac. Even then, expect a small surface area.
- **NOT remote-buildable.**
- **Effort:** ~hour, but with a known low ceiling on what it can actually do.

### LLM-classified email watch
- **What:** Layer a Haiku-based classifier on top of the existing rules-based email watch. After the rules pass (sender allowlist + urgency keywords), any remaining new unread emails get a single short Haiku call: "is this urgent or does it require a direct personal response?" → if yes, included in the same notification batch. Catches contextual urgency that pure keyword/sender matching misses ("Hey can you call me when you get a chance" from a friend, etc.).
- **Why deferred:** The rules-only version (commit `3fc4584` + `bd1168a`) is in production today. We agreed to ship that first and only layer the LLM tier if the rules-only false-negative rate proves too high in real use. Wait for a few weeks of usage data before deciding.
- **Unblocks:** Pure code on top of the existing `_fire_email_watch` in `scheduler/triggers.py`. No new auth — uses the existing `ANTHROPIC_API_KEY`. **Remote-buildable.**
- **Cost:** Haiku at ~$1/M input tokens; ~50 unread emails/day × ~200 tokens each = ~$0.01/day.
- **Effort:** ~hour (classifier function + prompt + integration with the rules-pass-through, plus rate-limiting so a flurry of "yes" classifications batches into one notification not N pings).

### Discord transport
- **What:** Third option for `RELAY_TRANSPORT` alongside `imessage` and `telegram`. Discord bot you create at discord.com/developers/applications, allowlisted by user/server ID, supports text + image attachments (same vision flow as the other transports).
- **Why deferred:** Just hadn't gotten to it.
- **Unblocks:** Web setup (Discord Developer Portal → Application → Bot → token + invite link). Wire a `relay/discord_relay.py` that mirrors the structure of `telegram_relay.py`. Update `relay/sender.py` factory + `relay/run.py` dispatcher. **Remote-buildable.**
- **Effort:** ~hour.

### Slack transport
- **What:** Fourth option for `RELAY_TRANSPORT`. Slack bot in a workspace; useful for work-context messaging or for keeping the agent in a Slack DM. Allowlisted by Slack user ID.
- **Why deferred:** Not yet built; lower priority for personal use than Discord/Telegram unless you live in Slack for work.
- **Unblocks:** Web setup at api.slack.com/apps (create app → enable Socket Mode for polling, or webhooks for events → bot token + signing secret). Mirrors the relay/telegram_relay.py pattern. **Remote-buildable.**
- **Effort:** ~hour.

### SMS via Twilio transport
- **What:** Fifth option for `RELAY_TRANSPORT`. SMS bidirectional — universal reach (any phone, no app, no Apple/Google ecosystem dependency), bypasses every iMessage/DND/Focus quirk.
- **Why deferred:** Costs money (~$1/mo for a phone number + ~$0.01/msg outbound) and SMS is text-only (no image attachments → vision flow is unavailable). Worth it as a fallback / travel transport, not as primary.
- **Unblocks:** Twilio signup → phone number + Account SID + Auth Token. Webhook-based incoming messages (so the daemon needs to expose a public HTTPS endpoint — ngrok for dev, hosted for production). `relay/sms_relay.py` following the existing transport pattern. **Remote-buildable for the code; deployment needs a public URL.**
- **Effort:** ~hour for code; another hour for deployment story (ngrok or a real host).

### Group chat support in the iMessage relay
- **What:** Let you @-mention the agent in a family / work group iMessage thread (instead of only watching note-to-self chats), with a whitelist of allowed group chats and explicit @-mention triggering so the agent doesn't respond to every message in a group.
- **Why deferred:** Loop prevention is trickier in shared chats (your own outgoing messages from any device flow through too); the personality contract around safety is harder when third parties are reading.
- **Unblocks:** Pure code. **Remote-buildable.**
- **Effort:** ~hour.

### Dedicated agent identity
- **What:** Give the agent its own Apple ID or Google Voice number so its replies render as inbound (gray bubbles, "from someone else") instead of as your own outgoing messages in a self-chat. Also avoids the iCloud sync quirks that affect note-to-self threads.
- **Why deferred:** Setting up a fresh Apple ID requires signing in on a device (browser-only Apple ID creation has been restricted since 2022); Google Voice needs phone verification. Both want some local-device access.
- **Unblocks:** External account setup + iMessage configuration on the Mac.
- **NOT remote-buildable.**
- **Effort:** ~half-day end-to-end (account creation, device sign-in, relay reconfiguration).

## Operational improvements (not sub-agents)

These aren't user-facing capabilities but improve daily use.

- ~~Tighter morning brief / weekly review prompts~~ — shipped (synthetic prompts now have explicit char budgets and "skip empty sections" rule).
- ~~Tighter replies on very short user messages~~ — shipped (personality.md now requires one-word replies to one-word inputs).
- ~~Audit-log analytics tool~~ — shipped as `tools/analytics.py`.
- ~~"Query archive" tool~~ — shipped as the `archive` sub-agent.
- ~~Recurring reminders~~ — shipped (`remind_recurring` tool with daily / weekdays / weekly / monthly patterns).

## Pending verification

Things that are shipped but haven't been live-validated end-to-end yet.
Code is in main; just need a test session to confirm behavior.

### Telegram transport — live test
- **What:** Confirm `RELAY_TRANSPORT=telegram` works end-to-end from a
  bot created via @BotFather: text from phone → relay picks up →
  agent replies in Telegram → conversation is archived under
  `source='telegram'`. Also verify image attachments (a photo with
  caption goes through the vision flow). Also verify the scheduler
  delivers the morning brief to Telegram when the transport is
  switched.
- **Why deferred:** Implementation landed in commit `ca6e763` but
  hasn't been exercised against a real Telegram bot yet — current
  daily use is still on iMessage.
- **Unblocks:** Web setup at @BotFather (~3 min), find user id via
  @userinfobot, paste both into `.env`, set `RELAY_TRANSPORT=telegram`,
  reload the relay daemon. **Remote-buildable.**
- **Effort:** ~15 min for a full smoke test (text, image, scheduled
  brief, conversation rollover).

## Items considered but explicitly NOT planned

These came up in discussion but were decided against (so we don't waste time revisiting):

- **OpenWeatherMap** — redundant with the existing Open-Meteo weather sub-agent.
- **Tavily / Serper as alternative search** — Brave Search is already wired up; switching providers is a non-improvement.
- **Multiple email providers** — Gmail's enough for this principal.
- **Voice notes (TTS)** — out of scope without local audio playback wiring.
