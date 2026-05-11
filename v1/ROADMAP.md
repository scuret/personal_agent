# Roadmap

What's shipped, what's planned, and what each planned item needs to actually land.

## Shipped

19 sub-agents currently live: **memory, archive (aggregate analytics), todoist, gmail, calendar (read + write), drive, docs, sheets, weather, vision, notion, github, web (Brave search + URL fetch), youtube, dropbox, spotify, wikipedia, reddit (public read), reminders.**

Plus a **local admin web UI** at `http://127.0.0.1:8780`:
- FastAPI + Jinja2 + HTMX + Tailwind via CDN. No Node toolchain, no build step.
- Dashboard (daemon status, today's spend, pending reminders, upcoming fires, one-click trigger buttons)
- Web chat surface with SSE streaming, conversation continuity (shared archive with iMessage / Telegram / scheduler)
- History browser with per-conversation message threads + tool-call inspection
- Observability: cost report, behavioral analytics (hour/day, tools, slow turns, lengths), live token health, SSE-tailed daemon logs
- In-browser editors for `triggers.yaml` (live reload), `personality.md` (restart required), `.env` (secret-masked, restart required)
- Auto-started via `com.personal-agent.webui` LaunchAgent

Plus operational tooling and infrastructure:
- iMessage relay (contact + self mode, attributedBody decoder for DND-suppressed messages)
- Telegram relay (alternative transport, allowlisted user IDs, image-attachment support)
- Pluggable transport via `RELAY_TRANSPORT` + `relay/run.py` dispatcher
- Recurring reminders (daily / weekdays / weekly / monthly)
- Rules-based email-watch trigger (sender allowlist + urgency keywords, polled every N minutes)
- Real-time delivery-watch trigger (UPS / FedEx / Amazon / USPS / DHL — extracts tracking number + carrier-specific URL from the email body, logs as `delivery_today` facts for brief rollup)
- Email-watch → agent context handoff (`alerted_email` facts so the agent can recall the right email when the principal says "draft a response" from a different session)
- Morning brief + Sunday weekly review scheduler with wallclock-based catchup (survives Mac sleep)
- Briefs run on Opus 4.7 (model override per-trigger; relay stays on Sonnet)
- Todoist hallucination guard for briefs (Python fetches + categorizes by priority/status; the agent surfaces only entries from the injected authoritative block — light paraphrase allowed, invention forbidden)
- Conversational brief format (lowercase prose openers, weather woven in, email → todo synthesis, "📦 deliveries today:" section, overdue-P1 progress diff vs last fire)
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

### ~~Vector memory~~ — shipped (local sentence-transformers, `BAAI/bge-base-en-v1.5` default; messages + facts embed inline at archive; hybrid vector + LIKE re-rank in `memory_search_conversations` and `memory_recall_facts`; `tools/backfill_embeddings.py` for historical rows. Local-only, no API key — kept the fork-and-run story clean.)

### Stocks / crypto
- **What:** Sub-agent that returns price quotes, recent performance, basic fundamentals. "What's BTC at?" / "Show me NVDA's last 30 days."
- **Why deferred:** Not surfaced in real usage yet.
- **Unblocks:** Pick a provider — CoinGecko (no auth, crypto only) and/or Alpha Vantage (free key, stocks). Code only after that's chosen.
- **Remote-buildable** (CoinGecko needs no signup; Alpha Vantage takes ~2 min on the web).
- **Effort:** ~45 min.

### ~~Wikipedia~~ — shipped
### ~~Reddit (public read-only)~~ — shipped

### ~~Calendar writes~~ — shipped (create / update / delete events on `calendar.events` scope).
### ~~Drive / Docs / Sheets~~ — shipped (full read/write, single Google OAuth covers all three plus calendar writes).
### ~~Dropbox OAuth refresh flow~~ — shipped (`mcp_servers/dropbox_auth.py` runs the consent dance and caches a refresh token; access tokens auto-refresh in-process).
### ~~Spotify~~ — shipped (search / playback / queue / playlists / devices; refresh-token flow via `mcp_servers/spotify_auth.py`).

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

## Suggestion pile (not yet planned)

Sub-agent ideas surfaced in conversation but not yet scoped onto the
planned list. Kept here so they don't get dropped between sessions.
Each entry has enough metadata to scope when promoted to "Planned."

### Apple-native (AppleScript, zero auth — local to the Mac)
- **Apple Reminders.app** — bridge native iOS/macOS Reminders into the agent. Useful alongside Todoist for the lists that live in Reminders.app (Siri-created reminders, family shared lists). ~hour.
- **Apple Notes.app** — read/append notes by title. "What did I write in my [X] note?" / "Append this to my running list." ~hour.
- **Apple Photos.app** — search the local photo library by date or content tag. ~hour.
- **Apple Music.app** — playback control alongside Spotify if you use both. ~45 min.
- **Apple Mail.app** — search/draft against non-Gmail accounts if you have any. ~hour.

### Information sources
- **News headlines** — NYT or AP API; slots into the morning brief as a "what's happening" section. API key, free tier. ~45 min.
- **Maps / places** — Google Places or OpenStreetMap Nominatim for "nearest X" / "drive time to Y" / "what's open near me." Google needs an API key (free tier); OSM is no-auth. ~hour.

### Finance
- **SimpleFIN banking** — read-only account balances + recent transactions. Open standard, personal-friendly, flat $1.50/month for all accounts. Could slot into the brief ("checking is at $X, $Y spent on groceries this week"). ~hour.

### Glue / utilities
- **IFTTT / Zapier webhooks** — generic glue layer for "send my agent X from Y service." Lets you wire any service that supports webhooks. ~45 min.
- **Pocket / Instapaper** — surface saved-for-later reading; could slot into a weekly review. OAuth. ~hour.
- **1Password CLI** — search passwords / secure notes via the `op` CLI. Treat carefully — credentials never go into agent output; the agent only confirms presence and surfaces metadata. ~hour.

### Health / wellness
- **Eight Sleep** — sleep score, HRV, heart rate, respiratory rate, bed/room temp, autopilot status. Slots cleanly into the morning brief ("slept 6h 42m, score 78, HRV down 4 from your week avg"). Uses the community-maintained `pyEight` library — email/password auth with refresh tokens. **Caveat:** unofficial API; could break if Eight Sleep changes endpoints. The sleep-context add to the brief makes it worth the maintenance risk. ~hour.

## Operational improvements (not sub-agents)

These aren't user-facing capabilities but improve daily use.

- ~~Tighter morning brief / weekly review prompts~~ — shipped (synthetic prompts now have explicit char budgets and "skip empty sections" rule).
- ~~Tighter replies on very short user messages~~ — shipped (personality.md now requires one-word replies to one-word inputs).
- ~~Drop markdown bold from agent output~~ — shipped (folded into the conversational brief rewrite; `personality.md` now bans markdown formatting entirely; brief PROMPTS use lowercase prose openers).
- ~~Audit-log analytics tool~~ — shipped as `tools/analytics.py`.
- ~~"Query archive" tool~~ — shipped as the `archive` sub-agent.
- ~~Recurring reminders~~ — shipped (`remind_recurring` tool with daily / weekdays / weekly / monthly patterns).

## Going-public prep (do BEFORE flipping the repo to public)

A checklist of items to complete before changing the GitHub repo from
private to public. Most are remote-doable. Don't push the repo public
until everything in this section is done — once a public push lands,
the audit is irreversible (anyone can clone before you tighten things).

### 1. Scrub personal email history (REQUIRED)
- **What:** Commit `bd1168a` and the surrounding history contain 15
  real email addresses of friends, family, and colleagues from the
  era when `config/triggers.yaml` was tracked. Removing the file in
  a later commit doesn't remove the addresses from history — anyone
  cloning a public repo can `git log -p` and read them.
- **Action:** `pip install git-filter-repo` then
  `git filter-repo --path v1/config/triggers.yaml --invert-paths`
  to rewrite history with that file removed from every commit. Then
  force-push (only safe because nothing's been pushed yet).
- **Verify:** `git log --all -p | grep -i <your-personal-search-term>` should return zero
  matches (or substitute any other personal address).

### 2. Add a LICENSE
- **What:** Without a LICENSE file the default is "all rights
  reserved" — people can read but legally can't fork, modify, or
  reuse. For a personal-utility template MIT is conventional.
- **Action:** Drop a standard MIT LICENSE at the repo root with the
  copyright year + holder name.

### 3. Cost disclaimer in README
- **What:** Public visitors might assume this is a free demo. It's
  not — every Claude turn costs real money (~$0.05–0.10 typical;
  build-day spikes much higher). The README should say so up front
  so nobody runs it expecting free.
- **Action:** Add a short "Costs" section near the top of README.md
  noting Anthropic API usage costs + how `tools/cost_report.py` lets
  you watch spend.

### 4. Generalize README language for public audience
- **What:** Current README assumes "the principal" is a specific
  person and reads like internal documentation. For a public repo
  it should read like a template ("fork this for your own use",
  "this is a personal project — code is shared as-is").
- **Action:** Reword the lead paragraphs + add a "What this is /
  isn't" section. Drop any phrasing that assumes the reader is the
  original author.

### 5. Switch to GitHub noreply email for future commits
- **What:** Every existing commit has author `scuret <<your-noreply-email>>`.
  Going public exposes that email on every commit. Past commits would
  need filter-repo to fix; future commits can use a noreply alias.
- **Action:** Set local git config to a GitHub-provided noreply email
  (find at github.com/settings/emails). Optionally rewrite past
  commits with `git filter-repo --email-callback` for consistency.

### 6. Security disclosure path
- **What:** Public repos receive vulnerability reports. Without a
  designated path, reports may end up in public issues — bad.
- **Action:** Add a brief SECURITY.md or a "security disclosure"
  paragraph in README pointing at a non-public channel (private
  email, GitHub Security Advisory, etc.).

### 7. CONTRIBUTING.md (optional)
- **What:** If you'll accept issues/PRs from strangers, set
  expectations: scope of the project, how to redact secrets when
  filing bugs, code style.
- **Action:** Short Markdown file at repo root.

### 8. Repo metadata (optional)
- **What:** GitHub topics + description + a clean README banner
  improve discoverability.
- **Action:** Set via `gh repo edit` or the web UI: description like
  "personal AI agent on Claude Agent SDK with iMessage / Telegram
  surfaces"; topics like `claude`, `agent`, `imessage`, `telegram`,
  `personal-assistant`, `mcp`.

### 9. Final secret sweep
- **What:** Belt-and-suspenders before push.
- **Action:** Run `git log --all -p | grep -iE` against the secret
  patterns from `tools/token_health.py` (sk-ant-, ghp_, ntn_, sl.u.,
  AIzaSy, BSAGE0). Already verified clean as of commit `e2024ca`,
  but re-run after step 1 since filter-repo will have rewritten
  every commit hash.

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
