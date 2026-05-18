"""Config MCP server — lets the agent manage its own sub-agents from chat.

When the user texts "what sub-agents are on" / "disable Spotify" /
"set up Todoist", the agent calls these tools to read or mutate
.env. Mutations write to .env via the shared `_env_io.write_env_values`
helper; the env_watcher in each daemon notices the mtime change and
exits, LaunchAgent respawns picking up the new config.

Hard rule: NO credentials in chat. Setup links route to the local
web UI on the user's Mac — `http://127.0.0.1:8780/settings/connect/<name>`
— which is loopback-only, so the URL is safe to ship in chat history
(the iPhone clicking it wouldn't reach it anyway; the user opens it
on their Mac).

Sub-agent enablement uses a soft-disable convention:
  SUBAGENTS_DISABLED=<comma-separated list of names>
Credentials stay in .env; enable = remove from the list, disable =
add to the list. Non-destructive.

In-process SDK MCP server — shares no state with the web UI process
(reads .env from disk on every call to stay fresh after a write).
Wired in from agent_host.build_options.
"""

from __future__ import annotations

import os
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from tools.install import SUBAGENTS, SubAgent
from web.routes._env_io import read_env_dict, write_env_values


def _web_ui_base() -> str:
    """Compose the local web UI's origin from the current WEB_PORT env.

    Defaults to 8780 (dev install). The Otto Agents bundle uses 8790
    so it can coexist with a dev install on the same Mac without
    binding the same port. Always 127.0.0.1 — never bind to LAN
    (ROADMAP H3).
    """
    port = (os.environ.get("WEB_PORT") or "8780").strip() or "8780"
    return f"http://127.0.0.1:{port}"


# ─── Disabled-set helpers ──────────────────────────────────────────────────


def disabled_subagents_from_env(env: dict[str, str]) -> set[str]:
    """Parse the SUBAGENTS_DISABLED comma list."""
    raw = (env.get("SUBAGENTS_DISABLED", "") or "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()}


def _format_disabled(names: set[str]) -> str:
    # Stable ordering for diff hygiene in .env.
    return ",".join(sorted(names))


# ─── Status computation (reuses settings.py:_status_for logic shape) ───────


def _by_name() -> dict[str, SubAgent]:
    return {sa.name: sa for sa in SUBAGENTS}


def _status(sa: SubAgent, env: dict[str, str], disabled: set[str]) -> dict[str, Any]:
    """Compact status row for one sub-agent — what the agent shows in chat."""
    if sa.always_on:
        return {
            "name": sa.name,
            "description": sa.description,
            "state": "enabled",
            "always_on": True,
            "missing_env": [],
        }

    missing_env = [v for v in sa.env_vars if not env.get(v)]
    if sa.needs_google_oauth:
        # Google family — credentials.json check happens in the web UI;
        # from chat we just say "needs google oauth done at /settings".
        pass

    if sa.name in disabled:
        state = "disabled"
    elif missing_env:
        state = "not_configured"
    else:
        state = "enabled"
    return {
        "name": sa.name,
        "description": sa.description,
        "state": state,
        "always_on": False,
        "missing_env": missing_env,
    }


def _fmt_row(s: dict[str, Any]) -> str:
    marker = {"enabled": "●", "disabled": "○", "not_configured": "·"}.get(s["state"], "?")
    base = f"  {marker} {s['name']:<14} {s['state']:<15} {s['description']}"
    if s["state"] == "not_configured" and s["missing_env"]:
        base += f" (missing: {', '.join(s['missing_env'])})"
    return base


# ─── Setup-link generator ──────────────────────────────────────────────────


def setup_link_for(name: str) -> str:
    """The URL the user opens on their Mac to start a sub-agent's auth flow.

    Loopback-only — safe to surface in chat. Same /settings/connect/<name>
    endpoint the wizard / settings page already use.
    """
    return f"{_web_ui_base()}/settings/connect/{name}"


# ─── MCP server factory ────────────────────────────────────────────────────


def create_config_mcp_server() -> McpSdkServerConfig:
    """Build the in-process MCP server. No shared state — reads .env per call."""

    list_schema = {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["enabled", "disabled", "not_configured", "all"],
                "description": (
                    "Filter to one state. Default 'all'. 'enabled' = "
                    "credentials present and not in SUBAGENTS_DISABLED. "
                    "'disabled' = soft-disabled but credentials kept. "
                    "'not_configured' = no credentials."
                ),
            },
        },
        "required": [],
    }

    @tool(
        "config_list_subagents",
        (
            "List the agent's sub-agents with current state. Use when the "
            "user asks 'what's on' / 'what sub-agents do you have' / "
            "'what's available'. Returns name + state + 1-line description."
        ),
        list_schema,
    )
    async def config_list_subagents(args: dict[str, Any]) -> dict[str, Any]:
        env = read_env_dict()
        disabled = disabled_subagents_from_env(env)
        statuses = [_status(sa, env, disabled) for sa in SUBAGENTS]
        filt = args.get("state", "all")
        if filt != "all":
            statuses = [s for s in statuses if s["state"] == filt]
        if not statuses:
            return {"content": [{"type": "text", "text": "(no matching sub-agents)"}]}
        # Group: enabled / disabled / not_configured for readability.
        order = {"enabled": 0, "disabled": 1, "not_configured": 2}
        statuses.sort(key=lambda s: (order.get(s["state"], 99), s["name"]))
        body = "\n".join(_fmt_row(s) for s in statuses)
        legend = "\n\nlegend: ● enabled  ○ disabled  · not configured"
        return {"content": [{"type": "text", "text": body + legend}]}

    status_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Sub-agent name, e.g. 'todoist', 'spotify', 'gmail'.",
            },
        },
        "required": ["name"],
    }

    @tool(
        "config_subagent_status",
        (
            "Detailed status for one sub-agent: env vars present/missing, "
            "whether it's soft-disabled, what credentials would be needed "
            "to set it up. Use when the user asks 'is X on?' / 'what does "
            "X need?'."
        ),
        status_schema,
    )
    async def config_subagent_status(args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"].strip().lower()
        sa = _by_name().get(name)
        if not sa:
            return {
                "content": [{"type": "text", "text": f"unknown sub-agent: {name!r}"}],
                "is_error": True,
            }
        env = read_env_dict()
        disabled = disabled_subagents_from_env(env)
        s = _status(sa, env, disabled)
        lines = [
            f"{sa.name}: {s['state']}",
            f"  description: {sa.description}",
        ]
        if sa.capabilities:
            lines.append(f"  capabilities: {sa.capabilities}")
        if s["missing_env"]:
            lines.append(f"  missing env: {', '.join(s['missing_env'])}")
            lines.append(f"  setup URL (open on Mac): {setup_link_for(sa.name)}")
        if sa.needs_google_oauth:
            lines.append("  shares Google OAuth (Gmail/Calendar/Drive/Docs/Sheets)")
        if s["state"] == "disabled":
            lines.append(
                f"  soft-disabled via SUBAGENTS_DISABLED. Re-enable with "
                f"config_enable_subagent name={sa.name!r}."
            )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    enable_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Sub-agent name."},
        },
        "required": ["name"],
    }

    @tool(
        "config_enable_subagent",
        (
            "Enable a sub-agent. If it was soft-disabled, removes from "
            "SUBAGENTS_DISABLED. If credentials are missing, returns the "
            "setup URL the user should open on their Mac instead of "
            "toggling. After a successful toggle, the relay + scheduler "
            "daemons auto-restart within ~10s to pick up the change."
        ),
        enable_schema,
    )
    async def config_enable_subagent(args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"].strip().lower()
        sa = _by_name().get(name)
        if not sa:
            return {
                "content": [{"type": "text", "text": f"unknown sub-agent: {name!r}"}],
                "is_error": True,
            }
        if sa.always_on:
            return {"content": [{"type": "text", "text": (
                f"{name} is always-on — no toggle needed."
            )}]}
        env = read_env_dict()
        disabled = disabled_subagents_from_env(env)
        missing_env = [v for v in sa.env_vars if not env.get(v)]
        # If credentials are missing, the user needs to set them up
        # before enable means anything. Surface the setup URL.
        if missing_env or sa.needs_google_oauth:
            url = setup_link_for(name)
            return {"content": [{"type": "text", "text": (
                f"{name} needs setup before it can be enabled. Open this "
                f"URL on your Mac:\n\n  {url}\n\nThat starts the OAuth / "
                f"key-paste flow. After you finish, I'll auto-detect the "
                f"new credentials (env_watcher restarts me)."
            )}]}
        if name not in disabled:
            return {"content": [{"type": "text", "text": (
                f"{name} is already enabled. Nothing to do."
            )}]}
        disabled.discard(name)
        write_env_values({"SUBAGENTS_DISABLED": _format_disabled(disabled)})
        return {"content": [{"type": "text", "text": (
            f"enabled {name}. Daemons will restart within ~10s to pick "
            f"up the change."
        )}]}

    disable_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Sub-agent name."},
        },
        "required": ["name"],
    }

    @tool(
        "config_disable_subagent",
        (
            "Soft-disable a sub-agent. Adds it to SUBAGENTS_DISABLED in "
            ".env — credentials stay put, but the sub-agent stops loading "
            "on the next daemon restart (auto-triggered within ~10s)."
        ),
        disable_schema,
    )
    async def config_disable_subagent(args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"].strip().lower()
        sa = _by_name().get(name)
        if not sa:
            return {
                "content": [{"type": "text", "text": f"unknown sub-agent: {name!r}"}],
                "is_error": True,
            }
        if sa.always_on:
            return {"content": [{"type": "text", "text": (
                f"{name} is always-on and can't be disabled."
            )}]}
        env = read_env_dict()
        disabled = disabled_subagents_from_env(env)
        if name in disabled:
            return {"content": [{"type": "text", "text": (
                f"{name} is already disabled. Nothing to do."
            )}]}
        disabled.add(name)
        write_env_values({"SUBAGENTS_DISABLED": _format_disabled(disabled)})
        return {"content": [{"type": "text", "text": (
            f"disabled {name}. Credentials are kept; re-enable any time "
            f"with config_enable_subagent. Daemons will restart within "
            f"~10s."
        )}]}

    setup_link_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Sub-agent name."},
        },
        "required": ["name"],
    }

    @tool(
        "config_get_setup_link",
        (
            "Return the URL the user should open on their Mac to set up "
            "(or reconfigure) a sub-agent. The URL is loopback-only "
            "(127.0.0.1) — it's safe to share in chat. Use when the user "
            "says 'set up X' / 'reconnect X' / 'where do I configure X'."
        ),
        setup_link_schema,
    )
    async def config_get_setup_link(args: dict[str, Any]) -> dict[str, Any]:
        name = args["name"].strip().lower()
        if name not in _by_name():
            return {
                "content": [{"type": "text", "text": f"unknown sub-agent: {name!r}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": (
            f"Open this on your Mac: {setup_link_for(name)}\n"
            f"(loopback-only URL — won't work from your phone; you have "
            f"to open it on the Mac running the agent.)"
        )}]}

    return create_sdk_mcp_server(
        name="config",
        version="1.0.0",
        tools=[
            config_list_subagents,
            config_subagent_status,
            config_enable_subagent,
            config_disable_subagent,
            config_get_setup_link,
        ],
    )


def main() -> None:
    raise NotImplementedError(
        "config_server is in-process; instantiate via "
        "create_config_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
