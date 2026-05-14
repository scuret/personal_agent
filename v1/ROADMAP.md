# Roadmap

What's shipped, what's planned, and what each planned item needs to actually land.

## Shipped

27 sub-agents currently live: **memory, archive (aggregate analytics), todoist, gmail, calendar (read + write), drive, docs, sheets, weather, vision, notion, github, web (Brave search + URL fetch), youtube, dropbox, spotify, wikipedia, reddit (public read), reminders, reminders_apple, notes_apple, photos_apple, music_apple, mail_apple, maps (Google or OSM), eightsleep.**

Plus a **local admin web UI** at `http://127.0.0.1:8780`:
- FastAPI + Jinja2 + HTMX + Tailwind via CDN. No Node toolchain, no build step.
- Dashboard (daemon status, today's spend, pending reminders, upcoming fires, one-click trigger buttons)
- Web chat surface with SSE streaming, conversation continuity (shared archive with iMessage / Telegram / scheduler)
- History browser with per-conversation message threads + tool-call inspection
- Observability: cost report, behavioral analytics (hour/day, tools, slow turns, lengths), live token health, SSE-tailed daemon logs
- In-browser editors for `triggers.yaml` (live reload), `personality.md` (restart required), `.env` (secret-masked, restart required)
- Facts + Reminders CRUD (list + create + deactivate / cancel) — replaces the Phase-1 read-only viewers
- Settings dashboard at `/settings`: sub-agent status (configured / needs-auth / connected), one-click Connect buttons that spawn the matching `mcp_servers/*_auth.py` script and SSE-tail its stdout, plus a single button to render-and-bootstrap the four LaunchAgent plists
- Install wizard at `/install`: detects a fresh checkout (`.env` missing or empty `ANTHROPIC_API_KEY`), bootstraps `.env` from `.env.example`, and walks the user into the settings page with a first-run banner. Home redirects to `/install` when first-run state is detected
- Web chat image attachments: 📎 picker (cap 4 per turn), saves under `data/uploads/<conv_id>/`, prepends `[attachment: image at PATH (mime)]` markers same as iMessage / Telegram / Discord / Slack relays so the vision sub-agent flow is identical
- Auto-started via `com.personal-agent.webui` LaunchAgent

Plus operational tooling and infrastructure:
- iMessage relay (contact + self mode, attributedBody decoder for DND-suppressed messages)
- Telegram relay (alternative transport, allowlisted user IDs, image-attachment support)
- Pluggable transport via `RELAY_TRANSPORT` + `relay/run.py` dispatcher
- Recurring reminders (daily / weekdays / weekly / monthly)
- LLM-first email triage (one Haiku 4.5 call per non-automated unread email decides flag AND emits one or more action-shaped "ping items" with date/time, what-to-bring, decision needed; multi-event emails produce multiple pings; replaced the rules-based allowlist + urgency-keyword gate)
- Expected-arrivals gap detection (named watches: when an event is within `lead_time_days` and no email has arrived from the expected sender with the expected subject, ping with "no email from X yet" — daily-throttled per watch)
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

### ~~Vector memory~~ — shipped (local sentence-transformers, `BAAI/bge-base-en-v1.5` default; messages + facts embed inline at archive; hybrid vector + LIKE re-rank in `memory_search_conversations` and `memory_recall_facts`; `tools/backfill_embeddings.py` for historical rows. Local-only, no API key — kept the fork-and-run story clean.)

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

### ~~LLM-classified email watch~~ — shipped, then evolved into LLM-FIRST email triage. The current implementation runs every non-automated unread email through one Haiku call that BOTH classifies AND produces structured ping items (see `_triage_email_with_haiku` in `scheduler/triggers.py`). The previous allowlist + urgency-keyword gate has been removed; Haiku judges in context.

### ~~Discord transport~~ — shipped (`relay/discord_relay.py`; bot via developer portal, DM-only, allowlisted by user ID, image attachments via vision flow).
### ~~Slack transport~~ — shipped (`relay/slack_relay.py`; Socket Mode so no public URL needed, DM-only, allowlisted by Slack user ID, image attachments via vision flow).

### SMS via Twilio transport
- **What:** Fifth option for `RELAY_TRANSPORT`. SMS bidirectional — universal reach (any phone, no app, no Apple/Google ecosystem dependency), bypasses every iMessage/DND/Focus quirk.
- **Why deferred:** Costs money (~$1/mo for a phone number + ~$0.01/msg outbound) and SMS is text-only (no image attachments → vision flow is unavailable). Worth it as a fallback / travel transport, not as primary.
- **Unblocks:** Twilio signup → phone number + Account SID + Auth Token. Webhook-based incoming messages (so the daemon needs to expose a public HTTPS endpoint — ngrok for dev, hosted for production). `relay/sms_relay.py` following the existing transport pattern. **Remote-buildable for the code; deployment needs a public URL.**
- **Effort:** ~hour for code; another hour for deployment story (ngrok or a real host).

### ~~Group chat support (iMessage + Telegram)~~ — shipped
- `IMESSAGE_GROUP_CHATS` + `IMESSAGE_GROUP_TRIGGERS` make the iMessage relay listen to allowlisted group chats in addition to the primary 1:1 mode. Trigger substrings gate responses (default `@agent, hey agent, agent,`); replies route back to the originating chat via AppleScript chat-id send.
- `TELEGRAM_ALLOWED_CHAT_IDS` + `TELEGRAM_GROUP_TRIGGERS` do the same for Telegram. The bot's own `@<username>` is always accepted as a trigger (resolved via getMe at startup). Group/supergroup chats only fire on trigger match; private chats are unchanged.
- Loop prevention: iMessage outbound is prefixed with `OUTGOING_MARKER` (zero-width space), and group fetchers filter it out. Telegram bots can't accidentally hear themselves.
- `python -m relay.imessage_relay --check` lists every group visible in `chat.db` for easy `IMESSAGE_GROUP_CHATS` discovery.
- `config/personality.md` now has a Group chats section with etiquette: no private inbox contents, terser replies, no spam @-pings. Scheduled briefs / reminders still go to the primary 1:1 destination.
- **Follow-up (not yet planned):** equivalent channel support in `discord_relay.py` / `slack_relay.py`. Both are DM-only today; the trigger + chat-routing pattern from iMessage/Telegram is the obvious shape.

### Dedicated agent identity
- **What:** Give the agent its own Apple ID or Google Voice number so its replies render as inbound (gray bubbles, "from someone else") instead of as your own outgoing messages in a self-chat. Also avoids the iCloud sync quirks that affect note-to-self threads.
- **Why deferred:** Setting up a fresh Apple ID requires signing in on a device (browser-only Apple ID creation has been restricted since 2022); Google Voice needs phone verification. Both want some local-device access.
- **Unblocks:** External account setup + iMessage configuration on the Mac.
- **NOT remote-buildable.**
- **Effort:** ~half-day end-to-end (account creation, device sign-in, relay reconfiguration).

### Multi-LLM provider support (Anthropic + OpenAI + Gemini)
- **What:** Let the installer prompt for the LLM provider — Claude (current default), OpenAI ChatGPT, or Google Gemini — and have the rest of the agent stack work transparently regardless of which is configured. Primary driver is the public-template story: when the repo goes public, strangers should be able to use the provider they already have a subscription with.
- **Why deferred:** The agent's entire reasoning loop runs on `claude-agent-sdk` which is Anthropic-native by design. Supporting other providers means building a `BackendClient` abstraction layer that translates between three sets of provider primitives:
  - **Tool-call format.** All 26 MCP sub-agents register via `claude-agent-sdk`'s native MCP protocol. OpenAI uses "function calling," Gemini uses "function declarations" — each has different schema shapes for tool definitions, tool-call arguments, and tool results. Each MCP server would need a per-provider shim (or a translator at the SDK boundary).
  - **System prompt + prompt caching.** Anthropic's prompt cache is what makes our 50-fact `build_system_prompt` injection cheap. OpenAI's cache is similar but the API shape differs; Gemini has its own. Provider-specific cache discipline.
  - **PreToolUse safety hook.** The "never auto-send email" hook is implemented as a `claude-agent-sdk` PreToolUse hook. OpenAI's API doesn't have an equivalent — we'd need to wrap every tool-call execution in a Python pre-flight check. Gemini same.
  - **Streaming primitives.** `process_turn_stream`, the SSE chat surface, and the audit-log archival all wrap `ClaudeSDKClient.receive_response()`'s async-iterator shape. Each provider's streaming API is different.
  - **Reliability gap.** At the time of this writing, Claude is meaningfully better than GPT-4 / Gemini at long agentic tool-call chains (5+ tools across multiple sub-agents in a single brief fire). Multi-provider support needs to accept that the OpenAI / Gemini paths will hit lower tool-call success rates on briefs.
- **Unblocks:**
  - Define `BackendClient` Protocol in a new `agent_host/backends/` package
  - Implement `AnthropicBackend` first (just wraps current behavior); confirm zero-regression
  - Implement `OpenAIBackend` — translate MCP tool defs to function-calling JSON schemas, wrap tool execution loop, translate streaming chunks
  - Implement `GeminiBackend` — same shape, different schemas
  - Refactor every caller of `ClaudeSDKClient` / `build_options` / `process_turn` / `process_turn_stream` to go through the abstraction
  - Add `LLM_PROVIDER` env var (anthropic/openai/gemini) + `OPENAI_API_KEY` + `GEMINI_API_KEY`
  - Installer: add a provider-selection step (default anthropic)
  - Personality + safety hook: reimplement the no-send rule per backend (Python pre-flight wrapper rather than SDK hook)
  - Document the tool-call reliability trade-offs in README so forkers know what they're choosing
- **Remote-buildable.**
- **Effort:** ~2-3 weeks of focused work. Plus ongoing 3x maintenance cost for every new sub-agent or feature.
- **Status:** Not committed. We agreed to keep this on the roadmap as a "do before / alongside repo-going-public" item, but only if/when the public-template story becomes a priority. For personal use, Claude stays the default; switching providers via env var would just be extra surface to maintain.

## Suggestion pile (not yet planned)

Sub-agent ideas surfaced in conversation but not yet scoped onto the
planned list. Kept here so they don't get dropped between sessions.
Each entry has enough metadata to scope when promoted to "Planned."

### ~~Apple-native (AppleScript, zero auth — local to the Mac)~~ — shipped
- ~~Apple Reminders.app~~ — `mcp_servers/reminders_apple_server.py` (list_lists / list / create / complete / delete)
- ~~Apple Notes.app~~ — `mcp_servers/notes_apple_server.py` (list / search / read / append / create)
- ~~Apple Photos.app~~ — `mcp_servers/photos_apple_server.py` (read-only; list_albums / recent / search_by_date / get_album. ML face/object/place search remains out of scope.)
- ~~Apple Music.app~~ — `mcp_servers/music_apple_server.py` (now_playing / play / pause / next / previous / search_and_play / list_playlists). Coexists with Spotify.
- ~~Apple Mail.app~~ — `mcp_servers/mail_apple_server.py` (list_accounts / search / read / draft_reply / draft_new). **Never sends** — same safety contract as Gmail.

### Information sources
- ~~**Maps / places**~~ — shipped (`mcp_servers/maps_server.py` with provider abstraction: Google Maps Platform when `GOOGLE_MAPS_API_KEY` set, free OpenStreetMap (Nominatim + OSRM) fallback. Tools: search_places, drive_time, geocode, reverse_geocode).

### Finance
- **SimpleFIN banking** — read-only account balances + recent transactions. Open standard, personal-friendly, flat $1.50/month for all accounts. Could slot into the brief ("checking is at $X, $Y spent on groceries this week"). ~hour.

### Glue / utilities
- **IFTTT / Zapier webhooks** — generic glue layer for "send my agent X from Y service." Lets you wire any service that supports webhooks. ~45 min.
- **Pocket / Instapaper** — surface saved-for-later reading; could slot into a weekly review. OAuth. ~hour.
- **1Password CLI** — search passwords / secure notes via the `op` CLI. Treat carefully — credentials never go into agent output; the agent only confirms presence and surfaces metadata. ~hour.

## Operational improvements (not sub-agents)

These aren't user-facing capabilities but improve daily use.

- ~~Tighter morning brief / weekly review prompts~~ — shipped (synthetic prompts now have explicit char budgets and "skip empty sections" rule).
- ~~Tighter replies on very short user messages~~ — shipped (personality.md now requires one-word replies to one-word inputs).
- ~~Drop markdown bold from agent output~~ — shipped (folded into the conversational brief rewrite; `personality.md` now bans markdown formatting entirely; brief PROMPTS use lowercase prose openers).
- ~~Audit-log analytics tool~~ — shipped as `tools/analytics.py`.
- ~~"Query archive" tool~~ — shipped as the `archive` sub-agent.
- ~~Recurring reminders~~ — shipped (`remind_recurring` tool with daily / weekdays / weekly / monthly patterns).

## Security enhancements

Output of a focused personal-data-exposure audit (2026-05-14). Three
parallel reviews covered (a) secrets / tokens / file permissions, (b)
data at rest (SQLite, logs, uploads), and (c) data in motion + the
web UI surface. 15 findings recorded below.

The user-facing **warnings** scaffolding (README privacy section,
`.env.example` banner, installer disclosure, web UI banner + /about/
privacy page) shipped alongside this section so anyone running the
package understands the risk profile before the deep fixes land.

### Active (planned for upcoming sessions)

Batch 1 (shipped 2026-05-14): H1, H3, M1, M2 plus the user-facing
warning scaffolding.

Batch 2 (shipped 2026-05-14): H5, M4, M5.

Remaining work:

- **H2** — encrypt the audit-log database (SQLCipher preferred,
  30-day `api_events` retention purge as fallback).
- **H4** — `git filter-repo` to scrub `config/triggers.yaml` from
  history (also blocks public push; overlaps with going-public #1).
- **M3** — group-chat third-party retention policy.

#### ~~H1. File-permission hardening on tokens, DB, and logs~~ — shipped
- Every token cache writer (`mcp_servers/dropbox_auth.py`,
  `spotify_auth.py`, `canva_auth.py`, `linkedin_auth.py`,
  `eightsleep_auth.py`, `google_auth.py`) now chmods its output to
  `0o600` immediately after writing.
- `memory/store.py` chmods `data/memory.sqlite` + its `-wal` / `-shm`
  companion files on every `MemoryStore.__init__`. Idempotent, so an
  upgrade-in-place automatically locks down an existing world-readable
  DB.
- `tools/rotate_logs.py` chmods both the rotated dated copy and the
  truncated live log to `0o600` on every rotation pass. Log-name list
  now covers all daemons (relay / scheduler / web / log-rotation).
- New `tools/repair_permissions.py` — one-shot fix for users with
  existing 0o644 files. `--dry-run` previews; default applies. Scans
  `.env`, `config/credentials.json`, `config/triggers.yaml`, plus
  every SQLite / pickle / token / log file under `data/`.

#### H2. Encrypt the audit-log database (preferred) or 30-day fallback
- **Risk:** `data/memory.sqlite` is a plaintext SQLite file holding
  every conversation, every fact extracted, and a verbatim copy of
  every Claude API payload in `api_events` (541 rows today, growing).
- **Fix (preferred):** Migrate to SQLCipher with a passphrase stored
  in the macOS Keychain. Existing rows are preserved; encryption is
  transparent at the DB layer.
- **Fallback:** If SQLCipher integration is fragile or too disruptive,
  add 30-day retention purge for `api_events` only. Preserve
  `messages`, `facts`, and `conversations` indefinitely so the
  conversation history and memory aren't lost.
- **Files:** `memory/store.py`, new `memory/encryption.py`, new
  `tools/migrate_db_encryption.py`.

#### ~~H3. Hard-bind the web UI to 127.0.0.1~~ — shipped
- `web/app.py` now ignores any `WEB_HOST` env override that isn't
  `127.0.0.1` unless the user has also set `WEB_ALLOW_LAN=1`. With
  the override blocked, a stderr line documents the override-attempt
  and what the user would need to set to allow it. With the override
  honored, a louder stderr warning explains that the UI has no auth /
  CSRF and recommends a firewall or reverse proxy in front of it.
- `.env.example` and the install disclosure both call out the
  two-step opt-in. CSRF tokens / Origin header checks / `/uploads`
  auth remain out of scope as acceptable risk for v1.

#### H4. Scrub `triggers.yaml` from git history
- **Risk:** `config/triggers.yaml` was tracked in commits `bd1168a`,
  `3fc4584`, `4284f35` with 15 real personal email addresses. Removed
  in `32c0fdd` but persists in history. Once the repo flips public,
  `git log -p` exposes everything irreversibly.
- **Fix:** Already on the going-public prep list as item #1.
  Reaffirmed here as a security blocker before any public push.
  `pip install git-filter-repo` then
  `git filter-repo --path v1/config/triggers.yaml --invert-paths`,
  then force-push.

#### ~~H5. Move `EIGHT_PASSWORD` to macOS Keychain~~ — shipped
- `keyring>=25.0` added to `pyproject.toml`. `mcp_servers/eightsleep_
  auth.py` now resolves the password by checking the macOS Keychain
  first (`personal_agent_eight_sleep` service, account = the user's
  email), then falls back to `EIGHT_PASSWORD` in `.env` with a stderr
  deprecation reminder.
- New `python -m tools.eightsleep_set_password` — interactive helper
  that prompts for the password (`getpass`), stores it in the
  keyring, and optionally clears `EIGHT_PASSWORD` from `.env`.
- Installer SubAgent entry updated: only `EIGHT_EMAIL` is required
  in `.env`; the help text points the user at the keychain tool.
  `.env.example` documents the Keychain-first / env-fallback choice.
- Stale `EIGHT_PASSWORD` env check in `scheduler/triggers.py`'s
  `_render_sleep_block` removed so Keychain-only setups still get
  the morning brief sleep section.

#### ~~M1. Extend `/config/env` masking to PII fields~~ — shipped
- `web/routes/config.py` now classifies a var as sensitive via
  `_is_sensitive()` — credential-shaped substrings (`KEY` / `SECRET` /
  `TOKEN` / `PASSWORD`) OR an explicit PII list (`EIGHT_EMAIL`,
  `TARGET_PHONE_NUMBER`, `SELF_HANDLES`, `USER_HOME_ADDRESS`) OR
  suffix patterns (`*_ALLOWED_USER_IDS`, `*_ALLOWED_CHAT_IDS`,
  `*_BRIEF_*`).
- Sensitive values render with `type=password` by default + a per-row
  👁 reveal button. The "secret" badge has been renamed to
  "sensitive" to cover the PII case. The global "reveal secrets"
  toggle in the header still works as a bulk override.
- PII values mask completely (`(masked)`) rather than showing a
  prefix, since a partial street name / phone area code is still a
  meaningful leak.

#### ~~M2. Truncate message previews in daemon logs + add retention~~ — shipped
- All `[in @ ...]` / `[out → ...]` / `[sent] ...` log statements in
  `relay/imessage_relay.py`, `relay/telegram_relay.py`,
  `relay/discord_relay.py`, `relay/slack_relay.py`, and
  `scheduler/triggers.py` now truncate the message body to 20 chars
  (down from 80). Combined with H1's `0o600` perms, this strictly
  limits how much message content leaks if logs are read by another
  local user.
- `tools/rotate_logs.py` retention was already enforced via
  `KEEP_DAYS = 7` (rotated logs older than 7 days get pruned on every
  rotation pass). Reaffirmed here and the log-name list now covers
  the web + log-rotation daemons in addition to relay + scheduler.

#### M3. Group-chat third-party retention policy
- **Risk:** When `IMESSAGE_GROUP_CHATS` is set, the relay archives
  messages from OTHER people in those groups indefinitely. Their
  content gets embedded for vector search, stored in `messages`, and
  is indistinguishable from the user's own data in the archive. No
  opt-in, no purge, no consent model.
- **Fix:** (a) Tag third-party messages with
  `metadata.is_third_party=true` at archive time. (b) Add
  `group_chat_retention_days` (default 30) with a scheduler purge
  job. (c) Web UI history filters third-party messages by default
  with a toggle. (d) Document in `personality.md` Group chats
  section so the agent can disclose it if asked in-group.
- **Files:** `relay/imessage_relay.py:481-528`, `memory/store.py`,
  `scheduler/triggers.py`, `web/routes/conversations.py`,
  `config/personality.md`.

#### ~~M4. Email-triage data flow disclosure + local-only opt-out~~ — shipped
- `EMAIL_TRIAGE_LOCAL_ONLY=true` env opt-out. When set, the
  scheduler short-circuits `_fire_email_watch` before any Anthropic
  call — no email content leaves the machine. Email pings stop;
  every other surface (brief, deliveries, expected arrivals) keeps
  working. Documented in `.env.example` with the trade-off spelled
  out (no LLM-based local fallback in v1; if you want pings you pay
  the Anthropic cost).
- Every `_fire_email_watch` run that classified any emails now logs
  an `email_triage_run` row to `api_events` with the counts. New
  `_render_email_triage_block` reads the last 24h of those rows and
  emits "📧 triaged N email(s) to Anthropic in the last 24h (M
  flagged)". The morning-brief assembly injects it with explicit
  rendering instructions so it lands at the bottom of the brief.
- README privacy section already documents the upstream data flow;
  the new visibility line surfaces it daily.

#### ~~M5. `data/uploads/` lifecycle~~ — shipped
- `web/routes/chat.py`'s `POST /chat/{conv_id}/end` now recursively
  deletes `data/uploads/{conv_id}/` after closing the conversation.
- New `_enforce_uploads_cap()` runs after every successful upload
  save. Sums the total size of `data/uploads/`, and if it exceeds
  `UPLOADS_TOTAL_CAP_MB` (default 500MB; set to 0 to disable),
  deletes oldest-first per-conversation directories until the tree
  is back under the cap.
- `.env.example` documents `UPLOADS_TOTAL_CAP_MB`. `personality.md`
  "Image attachments" tells the agent that older images may have
  been purged so it doesn't confabulate when asked about them.

### Recorded for future consideration (LOW — no implementation planned)

These are theoretical risks or quality-of-life improvements with
low practical exposure. Surfaced here so they're not dropped between
sessions, but not on the implementation plan.

- **L1. Token-rotation hygiene.** `tools/token_health.py` checks
  validity but never rotates. README warns "rotate quarterly" but
  it's manual. Future: `tools/rotate_tokens.py` skeleton + cadence
  reminders in the morning brief.
- **L2. Relay-destination sanity check.** `TARGET_PHONE_NUMBER` is
  used verbatim; a typo silently re-routes briefs. Future: format-
  validate at startup; warn on first send if destination changed.
- **L3. Maps `USER_HOME_ADDRESS` caching.** Every geocode query
  sends the home address to Google/OSM. Future: cache coordinates
  locally and only re-resolve when the address changes.
- **L4. Vector embedding reversibility.** BGE-base is a public model
  + raw float32 vectors stored alongside text. Low practical risk
  for v1; revisit if the corpus grows or sensitivity changes.
- **L5. Time Machine / iCloud Drive exclusion.** Mentioned in the
  README privacy section as a user recommendation but not enforced.

### Already strong (no change needed)

- `.gitignore` blocks `.env*`, `config/credentials.json`,
  `config/triggers.yaml`, `data/*.sqlite*`, `data/*.log`,
  `data/*_token.json`, `data/*.json`, `data/*.pickle`.
- PreToolUse hook + `tools=[]` + `strict_mcp_config=True` keep the
  agent from inheriting Claude Code's environment or shell access.
- "Never auto-send" enforced in personality + tool surface + SDK
  hook for both Gmail and Apple Mail.
- Group chats etiquette section in `personality.md`.

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

### ~~2. Add a LICENSE~~ — shipped
- MIT License at repo root (`/LICENSE`), 2026, Stephen Curet.

### ~~3. Cost disclaimer in README~~ — shipped
- "Costs" section near the top of `v1/README.md` covering Sonnet /
  Opus / Haiku tiers, typical daily spend, and how to watch it via
  `python -m tools.cost_report` or the web UI Observability page.

### ~~4. Generalize README language for public audience~~ — shipped
- Lead paragraphs rewritten with "personal project, shared as-is"
  framing. New "What this is / what it isn't" section. References to
  "the principal" in user-facing prose are reframed to "you" (the
  term-of-art usage inside `personality.md` is intentional and stays).

### 5. Switch to GitHub noreply email for future commits
- **What:** Every existing commit has author `scuret <<your-noreply-email>>`.
  Going public exposes that email on every commit. Past commits would
  need filter-repo to fix; future commits can use a noreply alias.
- **Action:** Set local git config to a GitHub-provided noreply email
  (find at github.com/settings/emails). Optionally rewrite past
  commits with `git filter-repo --email-callback` for consistency.

### ~~6. Security disclosure path~~ — shipped
- `SECURITY.md` at repo root. GitHub Security Advisory is the only
  supported channel (no maintainer email exposed). Scope + response
  expectations + hardening notes for forkers are spelled out.

### ~~7. CONTRIBUTING.md~~ — shipped
- `CONTRIBUTING.md` at repo root. "Welcome but lightly maintained"
  posture, secret-redaction guidance, ruff + mypy expectations, fork-
  vs-contribute-back guidance.

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
