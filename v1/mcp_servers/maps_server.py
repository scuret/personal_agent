"""Maps/Places MCP server.

Dispatches to Google Maps Platform if `GOOGLE_MAPS_API_KEY` is set in
env, falls back to OpenStreetMap (Nominatim + OSRM) otherwise. Both
providers implement the same MapsProvider interface; this server just
formats their results into MCP tool responses.

Tools (namespaced as mcp__maps__<name>):
  maps_search_places(query, lat?, lon?, radius_m?, limit?)
  maps_drive_time(origin, destination)
  maps_geocode(address)
  maps_reverse_geocode(lat, lon)

If `USER_HOME_ADDRESS` is set in env, the agent can use it as a default
origin for drive-time queries ("drive time to Annie Gunn's" defaults
origin=home).
"""

from __future__ import annotations

import os
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from mcp_servers.maps_providers import get_provider


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _home() -> str:
    return (os.environ.get("USER_HOME_ADDRESS") or "").strip()


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rem_m = divmod(minutes, 60)
    return f"{hours}h {rem_m}m" if rem_m else f"{hours}h"


def _fmt_distance(meters: int) -> str:
    if meters < 1000:
        return f"{meters}m"
    miles = meters / 1609.344
    return f"{miles:.1f}mi"


def create_maps_mcp_server() -> McpSdkServerConfig:
    @tool(
        "maps_search_places",
        (
            "Search for places by query. Optional lat/lon center for "
            "proximity search (otherwise text search globally). Returns "
            "name + address + coordinates for each hit."
        ),
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "radius_m": {"type": "integer", "minimum": 100, "maximum": 50000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    )
    async def search_places(args: dict[str, Any]) -> dict[str, Any]:
        provider = get_provider()
        try:
            results = provider.search_places(
                args["query"],
                lat=args.get("lat"),
                lon=args.get("lon"),
                radius_m=int(args.get("radius_m", 5000)),
                limit=int(args.get("limit", 10)),
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"maps search failed ({provider.name}): {e}")
        if not results:
            return _ok("(no places found)")
        lines = [f"[provider: {provider.name}]"]
        for r in results:
            line = f"- {r.get('name', '')}"
            if r.get("address"):
                line += f"\n    {r['address']}"
            rating = r.get("rating")
            if rating is not None:
                line += f"  ★ {rating}"
                if r.get("user_ratings_total"):
                    line += f" ({r['user_ratings_total']})"
            lines.append(line)
        return _ok("\n".join(lines))

    @tool(
        "maps_drive_time",
        (
            "Driving distance + duration between two locations. Origin "
            "and destination can be addresses or place names. Pass "
            "'home' as origin to use USER_HOME_ADDRESS (if set)."
        ),
        {
            "type": "object",
            "properties": {
                "origin": {"type": "string"},
                "destination": {"type": "string"},
            },
            "required": ["origin", "destination"],
        },
    )
    async def drive_time(args: dict[str, Any]) -> dict[str, Any]:
        provider = get_provider()
        origin = args["origin"]
        if origin.strip().lower() == "home":
            home = _home()
            if not home:
                return _err(
                    "origin='home' but USER_HOME_ADDRESS isn't set in .env"
                )
            origin = home
        try:
            res = provider.drive_time(origin, args["destination"])
        except Exception as e:  # noqa: BLE001
            return _err(f"maps drive_time failed ({provider.name}): {e}")
        text = (
            f"[provider: {provider.name}]\n"
            f"{res.get('summary', '')}\n"
            f"distance: {_fmt_distance(res['distance_m'])}\n"
            f"duration: {_fmt_duration(res['duration_s'])}"
        )
        return _ok(text)

    @tool(
        "maps_geocode",
        "Convert an address string to latitude/longitude.",
        {
            "type": "object",
            "properties": {"address": {"type": "string"}},
            "required": ["address"],
        },
    )
    async def geocode(args: dict[str, Any]) -> dict[str, Any]:
        provider = get_provider()
        try:
            res = provider.geocode(args["address"])
        except Exception as e:  # noqa: BLE001
            return _err(f"maps geocode failed ({provider.name}): {e}")
        if not res:
            return _err(f"address not found: {args['address']}")
        return _ok(
            f"[provider: {provider.name}]\n"
            f"{res['formatted_address']}\n"
            f"lat: {res['lat']}, lon: {res['lon']}"
        )

    @tool(
        "maps_reverse_geocode",
        "Convert lat/lon to a human-readable address.",
        {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
            },
            "required": ["lat", "lon"],
        },
    )
    async def reverse_geocode(args: dict[str, Any]) -> dict[str, Any]:
        provider = get_provider()
        try:
            res = provider.reverse_geocode(
                float(args["lat"]), float(args["lon"])
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"maps reverse_geocode failed ({provider.name}): {e}")
        if not res:
            return _err(f"no address found for ({args['lat']}, {args['lon']})")
        return _ok(
            f"[provider: {provider.name}]\n"
            f"{res['formatted_address']}"
        )

    return create_sdk_mcp_server(
        name="maps",
        version="1.0.0",
        tools=[search_places, drive_time, geocode, reverse_geocode],
    )


def main() -> None:
    raise NotImplementedError(
        "maps_server is in-process; instantiate via create_maps_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
