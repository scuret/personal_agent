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
- **Guided install wizard at `/wizard` (2026-05-15)** — 12-step linear flow for non-technical users. Each step has a "what you'll do" + "why it matters" header, plain-English form, a link to the matching `SETUP.md` section for provider click-paths, and (where applicable) a "Verify" button that SSE-streams `--check` output. Gates derived from `.env` + filesystem + `data/.install_progress.json`. The sub-agent picker uses a new `SubAgent.capabilities` field (paragraph per integration) so users can decide based on product-level value, not just integration surface. Fast-path for already-configured installs skips straight to the Done screen. CLI installer (`./install.sh`) still works for power users but the wizard banner is the recommended path. Full provider walkthroughs (Anthropic, all 5 transports, Google Cloud, 11 optional sub-agents, scheduler, LaunchAgents) live in `SETUP.md` at repo root, mirrored at `/about/setup`
- Web chat image attachments: 📎 picker (cap 4 per turn), saves under `data/uploads/<conv_id>/`, prepends `[attachment: image at PATH (mime)]` markers same as iMessage / Telegram / Discord / Slack relays so the vision sub-agent flow is identical
- Transport picker at `/settings/transports` — guided radio-button UI for the 5 transports (iMessage / Telegram / Discord / Slack / SMS). Per-transport field metadata in `web/routes/settings_transports.py` (one source of truth) drives the form; save writes to `.env` while preserving comments; verify button SSE-streams `python -m relay.<x>_relay --check`.
- **Chat-driven sub-agent management (2026-05-17)** — the agent can list / enable / disable / status / get-setup-link for any sub-agent from chat via the new `config_*` MCP tools. Soft-disable model: `SUBAGENTS_DISABLED` env var; credentials are never destructively edited. NO credentials in chat — sensitive setup routes to `http://127.0.0.1:8780/settings/connect/<name>` which the user opens on their Mac. Daemons auto-restart via a `tools/env_watcher.py` mtime poll on `.env` (relay + scheduler exit cleanly on change; LaunchAgent KeepAlive respawns with the new env in ~10s).
- **Per-trigger learning loop (2026-05-17, Phase 1)** — the agent captures user corrections via new `learning_*` MCP tools: positive/negative examples per trigger (`email_triage`, `morning_brief`, `weekly_review`) live in a new `trigger_examples` SQLite table. At call time, `scheduler/trigger_prompts.render_examples_block` prepends up to 3 positive + 3 negative recent examples to each trigger's prompt. Soft-delete via web UI at `/learning` or chat command. Brief / weekly-review feedback flow: agent fetches the last fire's assembled prompt via `learning_get_last_trigger_fire` before recording the correction, so `input_payload` reflects what the trigger actually saw. **Phase 2** (delivery_watch + expected_arrivals LLM gating) deferred — those triggers are pure string matching today and need a Haiku gate added before learning is meaningful.
- Auto-started via `com.personal-agent.webui` LaunchAgent

Plus operational tooling and infrastructure:
- iMessage relay (self / contact / **dedicated-identity** modes, attributedBody decoder for DND-suppressed messages, `--list-services` diagnostic for picking the agent's iMessage service in dedicated mode)
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

### ~~SMS via Twilio transport~~ — shipped (code; user-side deployment pending)
- `relay/sms_relay.py` hosts a FastAPI app at `POST /sms/webhook`
  on `127.0.0.1:8781` (configurable via `SMS_WEBHOOK_PORT`). Webhook
  POSTs are HMAC-signed by Twilio; the relay validates the
  signature via `TWILIO_AUTH_TOKEN` before processing.
- Allowlist by sender phone number (`SMS_ALLOWED_NUMBERS`).
- Replies via Twilio REST API with line-aware (then character-aware
  fallback) splitting at 1500 chars / segment.
- `relay/sender.py` + `relay/run.py` dispatch SMS as the fifth
  `RELAY_TRANSPORT`. The `twilio>=9.0` dep is in `pyproject.toml`.
- `.env.example` documents the full setup (Twilio signup → number →
  credentials → allowlist + ngrok for dev / reverse proxy for prod).
- Outstanding for the user: Twilio account / number purchase,
  ngrok or reverse-proxy setup to expose the webhook URL, then
  paste the URL into Twilio's "A MESSAGE COMES IN" field. The
  code is ready to receive; the public-reachability piece is the
  one bit that can't be auto-configured.

### ~~Group chat support (iMessage + Telegram)~~ — shipped
- `IMESSAGE_GROUP_CHATS` + `IMESSAGE_GROUP_TRIGGERS` make the iMessage relay listen to allowlisted group chats in addition to the primary 1:1 mode. Trigger substrings gate responses (default `@agent, hey agent, agent,`); replies route back to the originating chat via AppleScript chat-id send.
- `TELEGRAM_ALLOWED_CHAT_IDS` + `TELEGRAM_GROUP_TRIGGERS` do the same for Telegram. The bot's own `@<username>` is always accepted as a trigger (resolved via getMe at startup). Group/supergroup chats only fire on trigger match; private chats are unchanged.
- Loop prevention: iMessage outbound is prefixed with `OUTGOING_MARKER` (zero-width space), and group fetchers filter it out. Telegram bots can't accidentally hear themselves.
- `python -m relay.imessage_relay --check` lists every group visible in `chat.db` for easy `IMESSAGE_GROUP_CHATS` discovery.
- `config/personality.md` now has a Group chats section with etiquette: no private inbox contents, terser replies, no spam @-pings. Scheduled briefs / reminders still go to the primary 1:1 destination.
- **Discord channel + Slack channel support** — shipped 2026-05-15. Same trigger + allowlist shape as iMessage/Telegram. `DISCORD_ALLOWED_CHANNEL_IDS` + `DISCORD_GROUP_TRIGGERS` for Discord; `SLACK_ALLOWED_CHANNEL_IDS` + `SLACK_GROUP_TRIGGERS` for Slack. Slack also requires adding `message.channels` / `message.groups` / `message.mpim` to the app's Event Subscriptions — documented in `relay/slack_relay.py` and `.env.example`.

### ~~Dedicated agent identity~~ — code shipped 2026-05-16; user-side account setup is the remaining manual step
- The relay now has a third mode (`IMESSAGE_MODE=dedicated`) alongside `self` and `contact`. The agent's Apple ID signs in to Messages.app on the same Mac as the user's; the daemon reads incoming messages from `IMESSAGE_USER_HANDLE` (same SQL shape as contact mode) and sends replies through the iMessage service whose `id` or `description` substring-matches `IMESSAGE_AGENT_APPLE_ID`. Replies render as inbound gray bubbles instead of self-chat outgoing.
- New diagnostic `python -m relay.imessage_relay --list-services` enumerates signed-in Messages.app services so the user can confirm the agent's Apple ID is registered and copy the matching identifier into `.env`. The existing `--check` now also validates dedicated-mode env vars and confirms the configured agent Apple ID matches a live iMessage service.
- Research findings on Apple ID creation (2026-05-16): browser-only creation IS still supported at account.apple.com, but the new account needs a one-time sign-in on a device (the user's own Mac is fine) to activate iMessage, plus a real mobile phone number for SMS verification (Google Voice / VOIP blocked), plus mandatory irreversible 2FA. Multiple Apple IDs per person is not prohibited by Apple's ToS, but the iCloud Terms' broad "no automated means, like scripts" clause makes this gray area. No public bans of single-user low-volume two-IDs-one-person setups; all known bans are commercial / high-volume / multi-user. Full walkthrough + risk callout in `SETUP.md#imessage-dedicated-identity`.
- User-side remaining: create the second Apple ID, sign in on Messages.app, paste the user-handle + agent-Apple-ID into `.env`, smoke-test. Code is ready to receive.
- **NOT remote-buildable** for the user-side account creation step (Apple wants real-mobile SMS verification + a one-time device sign-in for new IDs).

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

Batch 3 (shipped 2026-05-14): H2 (fallback path — audit-log
retention purge; SQLCipher path deferred to future), M3.

Batch 4 (shipped 2026-05-14): H4 file removal — `git filter-repo`
rewrote all 67 commits; the 15-email allowlist is no longer in any
historical diff. Residual placeholder references in tracked docs
fold into going-public #9 (final secret sweep).

Remaining work: none in this section. The smaller cleanups for
documentation references happen as part of going-public #9.

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

#### ~~H2. Audit-log retention~~ — shipped (fallback path)
- SQLCipher whole-DB encryption was the preferred fix but doesn't
  have precompiled wheels for arm64 macOS + Python 3.13 yet
  (`sqlcipher3-binary` v0.5.3 only ships cp36–cp312 wheels;
  building from source needs `brew install sqlcipher` first, a
  per-machine prereq we don't want to push on forkers). Per the
  user's pre-authorized fallback, shipped the 30-day `api_events`
  retention purge instead.
- New `MemoryStore.purge_api_events(older_than_days)` deletes audit-
  log rows older than the threshold. Conversations / messages /
  facts / reminders are untouched.
- New `_fire_api_events_purge` runs daily (via the same throttle
  pattern as `_fire_third_party_purge`). Configured via
  `audit_log.audit_log_retention_days` in `triggers.yaml` (default
  30; set to 0 to disable).
- Combined with H1's `0o600` perms + FileVault, this keeps the
  blast radius of a stolen-laptop event bounded to the last 30 days
  of Claude payloads instead of the indefinite-growth status quo.
- **Future (not planned):** real DB encryption via SQLCipher once
  upstream ships arm64-Python-3.13 wheels, OR migrating the
  `api_events` payload column to per-row symmetric encryption with
  a Keychain-stored key. Tracked here for visibility; no
  implementation date.

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

#### ~~H4. Scrub `triggers.yaml` from git history~~ — shipped (fully)
- `git filter-repo --path v1/config/triggers.yaml --invert-paths`
  rewrote all 67 commits. The original 15-email allowlist no longer
  appears in `git log -p` for any commit.
- Three follow-up `--blob-callback` / `--message-callback` passes
  scrubbed residual references in tracked content + commit messages:
  the `expected_sender` example in `triggers.yaml.example`, the
  `sender_label` "Kara" string in the same file + a comment in
  `scheduler/triggers.py`, the `📧 from Daleesa` example in
  `personality.md`, and the `📧 from Daleesa` block in a commit
  message body. All genericized to neutral placeholders
  (`chair@example.org`, `"Chair"`, `Alex`).
- Final state: `grep -i` over `git log --all -p` for any of the
  original 18 personal-name / domain tokens returns **0 matches**.
- HEAD moved through `bdb7a16` → `229de80` → ... → `fe6a744`
  across the rewrites. Every prior hash changed.
- Repo backup saved at
  `~/personal_agent_backup_before_H4_<timestamp>.tgz` (283 MB)
  before the rewrite, in case rollback is ever needed.
- `origin` remote was wiped by filter-repo (its safety default) on
  each pass and re-added at the end.
- Force-push to GitHub is the next step but is **deferred until the
  user is ready to push**. The local repo is in the rewritten state
  and will not match any pre-existing remote history. When ready:
      git push --force origin main
- Note: `v1/config/triggers.yaml` (live, gitignored) still contains
  the user's actual expected-arrivals watch with the real sender
  email. That file is gitignored and never enters history — no leak.

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

#### ~~M3. Group-chat third-party retention policy~~ — shipped
- `messages` schema gained an `is_third_party INTEGER DEFAULT 0`
  column via the existing defensive-migration pattern.
- `MemoryStore.append_message` accepts `is_third_party: bool`.
  `process_turn` / `process_turn_stream` pass it through so any
  transport can tag the row.
- iMessage relay's `_fetch_group` sets the flag based on chat.db's
  `is_from_me`: messages from other group members → tagged.
  The principal's own group messages (from any device on their
  Apple ID) stay un-flagged.
- New `MemoryStore.purge_third_party_messages(older_than_days)` +
  `_fire_third_party_purge` scheduler fire (daily-throttled).
  Configured via `group_chat.group_chat_retention_days` in
  `triggers.yaml` (default 30; set to 0 to keep forever).
- Web UI `/history/<conv_id>` hides third-party rows by default
  with a banner showing the hidden count and a "show them" toggle
  (`?show_third_party=1`). When shown, third-party messages render
  with amber borders + a "group member" role label so they're
  visually distinct from the principal's own content.
- `personality.md` Group chats section tells the agent to disclose
  the retention policy honestly if a group member asks.

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

## Going-public prep — ✅ complete (2026-05-14)

The repo flipped to public at https://github.com/scuret/personal_agent
on 2026-05-14 after every item in this checklist was confirmed shipped.
All entries below are struck through with shipped notes for the record.

**Residual caveat — old commits via direct URL:** GitHub keeps
unreachable commits (the pre-rewrite `bdb7a16`-era hashes) accessible
via direct URLs like `github.com/scuret/personal_agent/commit/<hash>`
for up to 90 days even after a force-push removes them from history.
If you need the old leaky commits gone immediately, contact GitHub
Support and request garbage collection. After ~90 days they're
auto-pruned. For most threat models this is acceptable — discovering
the hashes requires guessing 7-char SHAs.

### ~~1. Scrub personal email history~~ — shipped
- Full scrub completed 2026-05-14 in 4 filter-repo passes (1 path
  removal + 2 blob-callback content replacements + 1 message-callback
  for a commit-message body). The original 15-email allowlist AND
  every residual personal-name reference in tracked content / commit
  messages have been genericized to neutral placeholders.
- Verification: `grep -i` over `git log --all -p` for the 18
  original tokens (daleesa*, kdouglass, bcuret, tfreeman, kingew4,
  matthew.d.wohl, jonathandahlberg, jessicaandrelevich, alamb,
  kdennis, awaide, greglaws, porschedriver, jasoncasey, ericfritsche,
  bellthe2nd, @fulton-school, @monetagroup, @empower, @lumiconconsulting,
  "Kara") returns 0 matches.
- See ROADMAP "Security enhancements → H4" for the per-pass detail.
- **Force-push deferred** — the local repo is in the rewritten
  state and won't match the (currently non-existent) origin. When
  ready to push: `git push --force origin main` after confirming
  no other clones / collaborators exist.

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

### ~~5. Switch to GitHub noreply email for future commits~~ — shipped
- Local `git config user.email` is now set to the GitHub-provided
  noreply alias (`<id>+<login>@users.noreply.github.com`). Future
  commits use it automatically.
- All 68 historical commits also rewritten via
  `git filter-repo --email-callback` to use the same noreply alias
  in their author + committer fields. `git log --format='%ae' | sort -u`
  returns one unique value — the noreply form. No real personal
  email is exposed in any commit on this branch.

### ~~6. Security disclosure path~~ — shipped
- `SECURITY.md` at repo root. GitHub Security Advisory is the only
  supported channel (no maintainer email exposed). Scope + response
  expectations + hardening notes for forkers are spelled out.

### ~~7. CONTRIBUTING.md~~ — shipped
- `CONTRIBUTING.md` at repo root. "Welcome but lightly maintained"
  posture, secret-redaction guidance, ruff + mypy expectations, fork-
  vs-contribute-back guidance.

### ~~8. Repo metadata~~ — shipped
- Description set: "Personal AI agent on the Claude Agent SDK —
  iMessage / Telegram / Discord / Slack surfaces, local web UI,
  27 sub-agents. Single-user, local-first on Mac."
- Topics: `agent`, `claude`, `claude-agent-sdk`, `imessage`,
  `local-first`, `mcp`, `personal-assistant`, `telegram`.

### ~~9. Final secret sweep~~ — shipped
- Re-ran `git log --all -p | grep -ciE` against the API-key
  patterns from `tools/token_health.py` (sk-ant-, ghp_, ntn_,
  sl.u., AIzaSy, BSAGE, xoxb-, xoxp-) after the H4 rewrites. Zero
  matches across the rewritten history.

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
