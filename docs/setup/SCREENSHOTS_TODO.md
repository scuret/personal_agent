# Screenshot handoff — to-do list for a future agent or human

`SETUP.md` and the install wizard reference image paths in this directory.
The PNG files don't exist yet. This file is the manifest for someone (or
another agent with browser-automation capability) to fill in.

## How to execute this task

Each entry below has:

- **Target path** — where the PNG should land (relative to repo root).
- **Provider URL** — where to navigate.
- **Click path** — how to reach the right screen once logged in.
- **What to capture** — exact crop / highlighted area.
- **Reference in SETUP.md** — the section the image illustrates, so you can
  verify the capture matches the surrounding text.

Each PNG should be:

- 1200–1600px wide max (compress with `pngquant` or `oxipng` after capture)
- Cropped to the relevant UI element + a bit of surrounding context (no full
  browser chrome — just the panel)
- Light theme if the provider offers it (better contrast in docs)
- No personal data visible (test account / blurred account name where the
  provider hides it poorly)

## Verification

After dropping each PNG in:

```
ls docs/setup/*.png    # confirm all listed files exist
grep -c "docs/setup/" SETUP.md  # count should match number of files
```

The wizard at `/about/setup` serves `SETUP.md` verbatim — the images will
render automatically once they're committed.

---

## Images to capture

### `docs/setup/discord-bot-create.png`

- **Provider URL:** <https://discord.com/developers/applications>
- **Click path:** New Application → name `personal_agent` → Save → **Bot** tab
- **Capture:** the Bot tab showing the **Reset Token** button + the
  **Message Content Intent** toggle (highlighted ON).
- **Used by SETUP.md section:** `transport-discord` step 2.

### `docs/setup/discord-oauth-url.png`

- **Provider URL:** same app → **OAuth2 → URL Generator**
- **Click path:** Scopes panel + Bot Permissions panel
- **Capture:** show `bot` scope checked + `Send Messages` / `Read Message
  History` / `Attach Files` permissions checked, plus the generated URL field.
- **Used by SETUP.md section:** `transport-discord` step 3.

### `docs/setup/discord-channel-id.png`

- **Provider URL:** Discord client itself, in a server you admin
- **Click path:** Settings → Advanced → enable Developer Mode → right-click
  any channel
- **Capture:** the right-click context menu with **Copy Channel ID** highlighted.
- **Used by SETUP.md section:** `transport-discord` optional channels block.

### `docs/setup/slack-app-create.png`

- **Provider URL:** <https://api.slack.com/apps>
- **Click path:** Create New App → From scratch
- **Capture:** the "App Name + Workspace" modal.
- **Used by SETUP.md section:** `transport-slack` step 1.

### `docs/setup/slack-socket-mode.png`

- **Provider URL:** same app → **Socket Mode**
- **Capture:** Socket Mode panel with **Enable** toggled on + the App-Level
  Token row showing `connections:write` scope.
- **Used by SETUP.md section:** `transport-slack` step 2.

### `docs/setup/slack-scopes.png`

- **Provider URL:** same app → **OAuth & Permissions**
- **Capture:** Bot Token Scopes list showing all 5: `chat:write`,
  `im:history`, `im:read`, `files:read`, `users:read`.
- **Used by SETUP.md section:** `transport-slack` step 3.

### `docs/setup/slack-events.png`

- **Provider URL:** same app → **Event Subscriptions**
- **Capture:** Enable Events ON + the Subscribe to bot events panel showing
  `message.im`. If you also want to capture channels, include
  `message.channels`, `message.groups`, `message.mpim`.
- **Used by SETUP.md section:** `transport-slack` step 4.

### `docs/setup/twilio-phone-number.png`

- **Provider URL:** <https://console.twilio.com>
- **Click path:** Phone Numbers → Manage → Active numbers → click your number
- **Capture:** the Messaging Configuration block showing the **A MESSAGE COMES
  IN** webhook field (with a placeholder URL).
- **Used by SETUP.md section:** `transport-sms` step 8.

### `docs/setup/google-cloud-credentials.png`

- **Provider URL:** <https://console.cloud.google.com>
- **Click path:** APIs & Services → Credentials → Create credentials → OAuth
  client ID → pick "Desktop app"
- **Capture:** the Create OAuth client ID screen with "Desktop app" selected
  and the name field filled in.
- **Used by SETUP.md section:** `google-cloud-project` step 4.

### `docs/setup/google-cloud-consent-screen.png`

- **Provider URL:** same project → APIs & Services → **OAuth consent screen**
- **Capture:** the OAuth consent screen config (User Type radio + the basic
  app-info form). Helpful: a second capture showing the "Test users" section
  where the user adds themselves.
- **Used by SETUP.md section:** `google-cloud-project` step 3.

### `docs/setup/gmail-api-enable.png`

- **Provider URL:** same project → APIs & Services → **Library**
- **Click path:** search "Gmail API" → click → Enable
- **Capture:** the Gmail API library page with the **Enable** button
  highlighted, OR (better) the post-enable confirmation showing API is on.
- **Used by SETUP.md section:** `google-cloud-project` step 2.

### `docs/setup/dropbox-app-create.png`

- **Provider URL:** <https://www.dropbox.com/developers/apps>
- **Click path:** Create app
- **Capture:** the Create app form with **Scoped access** selected, the
  permissions checklist visible (`files.metadata.read`, `files.content.read`,
  `sharing.read`).
- **Used by SETUP.md section:** `dropbox` step 2-3.

### `docs/setup/spotify-redirect-uri.png`

- **Provider URL:** <https://developer.spotify.com/dashboard>
- **Click path:** click your app → Settings → Redirect URIs panel
- **Capture:** the Redirect URIs panel with `http://127.0.0.1:8765` filled in
  (explicit reminder: NOT `localhost`).
- **Used by SETUP.md section:** `spotify` step 2.

---

## When you're done

```
git add docs/setup/*.png SETUP.md
git commit -m "docs: provider screenshots for SETUP.md"
git push
```

If any provider redesigns their UI and your capture rots, just re-shoot —
the SETUP.md text is the source of truth, the image is a supplement.

## Could be added later (optional)

- `gmail-share-with-integration.png` — Notion's "share page with integration"
  flow (useful for the `notion` section)
- `github-token-scopes.png` — GitHub personal-access-token scope checkboxes
- `imessage-full-disk-access.png` — macOS Privacy & Security panel showing
  Python.app added to Full Disk Access
- `eightsleep-keychain.png` — terminal output of
  `python -m tools.eightsleep_set_password`

Not blockers; the existing 13 cover the worst providers.
