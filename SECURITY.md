# Security Policy

## Reporting a Vulnerability

If you believe you've found a security issue in this project, please **do not
open a public GitHub issue**. Instead, report it privately via GitHub's
[Security Advisory](https://github.com/scuret/personal_agent/security/advisories/new)
flow.

That's the only supported channel — there's no separate security email. The
report goes only to maintainers and isn't visible publicly until a fix has
shipped.

## What to Include

A useful report usually has:

- A short description of the issue and the impact you think it has.
- Steps to reproduce, ideally with minimal config.
- Any sensitive data you happened to expose while testing — let me know so I
  can rotate it on my side too.

## Response Expectations

This is a personal project maintained on the side, so I can't promise an SLA.
What I can promise:

- I'll acknowledge the report within a few days.
- I'll work the fix in private and credit the reporter in the advisory once
  it's published, unless they prefer otherwise.

## Scope

In scope:

- Code in this repository (the v1 agent, MCP servers, web UI, scheduler,
  installer scripts).
- Default configurations shipped in this repo (e.g., the example .env, the
  example triggers.yaml).

Out of scope:

- Vulnerabilities in upstream dependencies — please report those to the
  upstream project. If a fix requires a version bump here, I'll handle that
  once the upstream fix lands.
- Issues that require pre-existing local access to a user's machine (the
  agent is designed for single-user, local-first deployment; it does not
  attempt to defend against an attacker who already owns the box).
- Social-engineering of an LLM to do something the user could have asked
  it to do directly (e.g., "trick the agent into reading a file the user
  has access to"). The agent is a user-authorized tool; it's not a sandbox.

## Hardening Notes for Forkers

A few things to be aware of if you run your own copy:

- `data/` and `.env` contain secrets — keep them out of git (they're already
  in `.gitignore`).
- The web UI binds to localhost by default. If you expose it on a LAN or
  the public internet, add your own auth in front of it; there is none
  built in.
- The agent has tool access to send messages, modify calendar/tasks, and
  read email. Run it as a user account whose scope matches what you want
  the agent to be able to do.
