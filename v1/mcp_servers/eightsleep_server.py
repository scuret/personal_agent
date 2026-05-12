"""Eight Sleep MCP server (unofficial API).

Read access to last-night sleep metrics + current bed state, plus a
single write tool to set bed temperature. Designed primarily to feed
the morning brief's sleep section ("slept 6h 42m, score 78, HRV down
4 from your week avg").

Tools (namespaced as mcp__eightsleep__<name>):
  eightsleep_last_night
  eightsleep_current_state
  eightsleep_set_temp(side, level)

CAVEAT: unofficial API. If Eight Sleep changes endpoints, this whole
sub-agent breaks. Each tool catches exceptions and returns a friendly
error rather than crashing the agent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.eightsleep_auth import auth_headers, user_id

# The Eight Sleep iOS app talks to a few different hosts; client-api
# is the long-standing one for user data + intervals (sleep sessions).
CLIENT_API_BASE = "https://client-api.8slp.net"
APP_API_BASE = "https://app-api.8slp.net"  # newer endpoints (autopilot, devices)
TIMEOUT_S = 20


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _get(url: str) -> dict[str, Any]:
    r = requests.get(url, headers=auth_headers(), timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _put(url: str, body: dict[str, Any]) -> dict[str, Any]:
    r = requests.put(
        url,
        headers={**auth_headers(), "Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()


def _fmt_minutes(seconds: int) -> str:
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _format_session(s: dict[str, Any]) -> str:
    """Render an Eight Sleep `interval` payload as a human-readable block."""
    score = s.get("score") or s.get("sleepFitnessScore", {}).get("total")
    duration_s = s.get("duration") or s.get("totalSleep", 0)
    hr = s.get("heartRate", {}).get("avg") or s.get("heartRate", {}).get("average")
    hrv = s.get("hrv", {}).get("avg") or s.get("hrv", {}).get("average")
    resp = s.get("respiratoryRate", {}).get("avg") or s.get("respiratoryRate", {}).get("average")
    bed_temp = s.get("bedTemperature", {}).get("avg") or s.get("bedTemperature", {}).get("average")

    lines: list[str] = []
    if duration_s:
        lines.append(f"time asleep: {_fmt_minutes(int(duration_s))}")
    if score is not None:
        lines.append(f"sleep score: {score}")
    if hr is not None:
        lines.append(f"avg heart rate: {hr:.0f} bpm")
    if hrv is not None:
        lines.append(f"avg HRV: {hrv:.0f} ms")
    if resp is not None:
        lines.append(f"avg respiratory rate: {resp:.1f} /min")
    if bed_temp is not None:
        lines.append(f"avg bed temp: {bed_temp:.1f}°F")
    return "\n".join(lines) or "(no readable metrics in last-night payload)"


def create_eightsleep_mcp_server() -> McpSdkServerConfig:
    @tool(
        "eightsleep_last_night",
        (
            "Sleep summary for the most recent completed session: score, "
            "time asleep, heart rate avg, HRV avg, respiratory rate avg, "
            "bed temp avg. Use this in morning-brief context."
        ),
        {"type": "object", "properties": {}, "required": []},
    )
    async def last_night(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            uid = user_id()
            data = _get(f"{CLIENT_API_BASE}/v1/users/{uid}/intervals")
        except Exception as e:  # noqa: BLE001
            return _err(f"eight sleep last_night failed: {e}")
        intervals = data.get("intervals") or []
        if not intervals:
            return _ok("(no sleep sessions found)")
        # Most-recent first per API ordering.
        latest = intervals[0]
        ts_start = latest.get("ts") or latest.get("timestamp")
        prefix = f"last session start: {ts_start}\n" if ts_start else ""
        return _ok(prefix + _format_session(latest))

    @tool(
        "eightsleep_current_state",
        (
            "Current bed state: in-bed or not, current bed temp, autopilot "
            "status. Useful for 'is the bed warming up yet' or 'am I in "
            "bed' queries."
        ),
        {"type": "object", "properties": {}, "required": []},
    )
    async def current_state(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            uid = user_id()
            me = _get(f"{CLIENT_API_BASE}/v1/users/{uid}")
        except requests.RequestException as e:
            return _err(f"eight sleep current_state failed: {e}")
        except Exception as e:  # noqa: BLE001
            return _err(f"eight sleep current_state failed: {e}")
        user = me.get("user") or me
        in_bed = user.get("currentDevice", {}).get("inBed")
        side = user.get("currentDevice", {}).get("side")
        device_id = user.get("currentDevice", {}).get("id")
        lines = []
        if in_bed is not None:
            lines.append(f"in bed: {'yes' if in_bed else 'no'}")
        if side:
            lines.append(f"side: {side}")
        if device_id:
            lines.append(f"device id: {device_id}")
        if not lines:
            lines.append("(no current device data)")
        return _ok("\n".join(lines))

    @tool(
        "eightsleep_set_temp",
        (
            "Set the bed temperature level for one side. `level` is -100 "
            "to 100 (-100=coldest, 0=neutral, 100=warmest), matching the "
            "Eight Sleep app's heat-level scale. Mutates external state; "
            "confirm with the principal before calling unless they "
            "explicitly asked."
        ),
        {
            "type": "object",
            "properties": {
                "side": {
                    "type": "string",
                    "enum": ["left", "right", "solo"],
                    "description": "Which side of the bed to set.",
                },
                "level": {
                    "type": "integer",
                    "minimum": -100,
                    "maximum": 100,
                },
            },
            "required": ["side", "level"],
        },
    )
    async def set_temp(args: dict[str, Any]) -> dict[str, Any]:
        side = args["side"]
        level = int(args["level"])
        try:
            uid = user_id()
            me = _get(f"{CLIENT_API_BASE}/v1/users/{uid}")
            device_id = (me.get("user") or me).get("currentDevice", {}).get("id")
            if not device_id:
                return _err("no current device id — bed may not be paired")
            _put(
                f"{CLIENT_API_BASE}/v1/devices/{device_id}",
                {f"{side}HeatingLevel": level},
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"eight sleep set_temp failed: {e}")
        return _ok(f"set {side} side heating level to {level}.")

    return create_sdk_mcp_server(
        name="eightsleep",
        version="1.0.0",
        tools=[last_night, current_state, set_temp],
    )


def main() -> None:
    raise NotImplementedError(
        "eightsleep_server is in-process; instantiate via create_eightsleep_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
