# Contributing

Thanks for taking a look. A few notes before you open a PR.

## Posture

This is a personal project that I share publicly so others can fork it,
copy ideas out of it, or adapt it for their own setup. I'm happy to take
contributions, but my maintenance time is limited and I will tend to
prioritize:

1. Fixes that affect me directly.
2. Small, self-contained improvements (clear scope, low review cost).
3. New sub-agents or transports that follow the existing patterns.

If you have a large idea, please open an issue first so we can talk about
scope before you spend time on a PR. Otherwise it's likely to sit.

## Forking vs. Contributing Back

For anything that's specific to your personal setup — your contacts in
`triggers.yaml`, your sub-agent allowlist, your scheduled brief times —
**fork and customize locally**. Don't send a PR back; those choices are
personal by design.

For anything that's structural (a new sub-agent, a bug fix, a new
transport, a docs improvement) — PRs welcome.

## Before You Open a PR

- Make sure your branch is based on a recent `main`.
- Run the linters and type checker — see `pyproject.toml` for the exact
  versions:

  ```bash
  cd v1
  ruff check .
  ruff format --check .
  mypy .
  ```

- If you touched the installer or any setup flow, walk through `install.sh`
  on a clean checkout and confirm it still works end-to-end.
- If you touched a sub-agent, add or update its entry in `tools/install.py`
  `SUBAGENTS` (that registry is the source of truth for everything that
  shows up in the web UI installer and the docs).

## Secret Hygiene

Please scrub anything personal before sending a PR. Things to check:

- No real email addresses, phone numbers, or contact names anywhere in
  code, configs, comments, or commit messages.
- No real API keys, OAuth client secrets, refresh tokens, or session
  tokens — even in test fixtures.
- No real iMessage chat IDs, Telegram user IDs, Discord/Slack IDs, or
  Eight Sleep device IDs.
- `config/triggers.yaml` and `.env` are gitignored — keep them that way.
  If your PR needs new fields, add them to `config/triggers.yaml.example`
  and `.env.example` instead.

If you're not sure whether something counts as a secret, leave it out and
I'll ask if I need it.

## Style

- Follow the existing code's shape — match the structure of nearby files
  rather than introducing new patterns.
- No drive-by refactors mixed with feature changes. One concern per PR.
- Comments explain *why*, not *what*. Most code shouldn't need any.
- Match the existing commit message style (short imperative subject; body
  only if context isn't obvious from the diff).

## Sub-agents

New sub-agents follow this shape:

- `mcp_servers/<name>_server.py` — the MCP server, one tool per public
  capability.
- `mcp_servers/<name>_auth.py` — only if there's OAuth or token refresh.
  Cache tokens under `data/`.
- Entry in `tools/install.py` `SUBAGENTS` with `name`, `env_vars`,
  `install_hint`, and any `always_on` / platform gating.
- Registration in `agent_host.py` behind a `_has_env(...)` or platform
  check, so the sub-agent only loads when its credentials are present.

The existing sub-agents are the canonical reference.

## License

By contributing, you agree that your contributions will be licensed under
the same MIT License that covers the rest of the project (see `LICENSE`).
