"""Weather MCP server — current conditions and multi-day forecast.

Uses Open-Meteo (https://open-meteo.com), a free public API that requires
no key. Two-step flow per request:
  1. Geocode the place name to lat/lon (geocoding-api.open-meteo.com).
  2. Pull weather data for that lat/lon (api.open-meteo.com).

Defaults to Fahrenheit, mph, and inches because the principal is in the
US. The agent can override via the `units` arg if needed.

Tools (namespaced as mcp__weather__<name>):

  weather_current(location)
      Now-cast: temp, feels-like, conditions, wind, humidity for a place.

  weather_forecast(location, days?)
      Daily highs/lows, conditions, precipitation chance for the next
      N days (default 5, max 16).
"""

from __future__ import annotations

from typing import Any

import requests
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 10

# WMO weather codes → short human-readable strings.
# https://open-meteo.com/en/docs#weathervariables
_WMO: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "light freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "light snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm w/ hail",
    99: "thunderstorm w/ heavy hail",
}


def _err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _condition(code: int) -> str:
    return _WMO.get(code, f"weather code {code}")


def _geocode(location: str) -> dict[str, Any] | None:
    """Resolve a place name to lat/lon. Returns None if not found.

    Open-Meteo's geocoder doesn't accept "City, State" as one query (it
    treats the whole string as a name and finds nothing). So we split on
    commas: the first part is the city to search, the remainder are
    qualifiers used to filter the multi-result list (matched against
    admin1, country, country_code, case-insensitive).
    """
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return None
    city = parts[0]
    qualifiers = [q.lower() for q in parts[1:]]

    resp = requests.get(
        GEO_URL,
        params={"name": city, "count": 10, "language": "en", "format": "json"},
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    if not results:
        return None
    if not qualifiers:
        return results[0]

    # Pick the first result that matches every qualifier somewhere in
    # admin1 / country / country_code.
    for r in results:
        haystack = " ".join(
            str(r.get(k, "")).lower() for k in ("admin1", "country", "country_code")
        )
        if all(q in haystack for q in qualifiers):
            return r
    # No exact qualifier match — fall back to the first result and let
    # the human notice if it's the wrong place.
    return results[0]


def _label(geo: dict[str, Any]) -> str:
    """Human-readable place label like 'Wildwood, FL, US'."""
    bits = [geo.get("name", "")]
    if geo.get("admin1"):
        bits.append(geo["admin1"])
    if geo.get("country_code"):
        bits.append(geo["country_code"])
    return ", ".join(b for b in bits if b)


def create_weather_mcp_server() -> McpSdkServerConfig:
    @tool(
        "weather_current",
        (
            "Get current weather (temp, feels-like, conditions, wind, humidity) "
            "for a place. Pass natural location names: 'Wildwood, FL', "
            "'St. Louis', 'Tokyo'. Defaults to Fahrenheit/mph/inches."
        ),
        {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Place name. City+state for US; city+country otherwise.",
                },
            },
            "required": ["location"],
        },
    )
    async def weather_current(args: dict[str, Any]) -> dict[str, Any]:
        try:
            geo = _geocode(args["location"])
        except requests.RequestException as e:
            return _err(f"weather geocode failed: {e}")
        if not geo:
            return _err(f"couldn't find location: {args['location']!r}")

        try:
            resp = requests.get(
                FORECAST_URL,
                params={
                    "latitude": geo["latitude"],
                    "longitude": geo["longitude"],
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "weather_code,wind_speed_10m,wind_direction_10m,is_day",
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "precipitation_unit": "inch",
                    "timezone": "auto",
                },
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"weather forecast failed: {e}")

        cur = (resp.json() or {}).get("current") or {}
        temp = round(cur.get("temperature_2m", 0))
        feels = round(cur.get("apparent_temperature", 0))
        humidity = round(cur.get("relative_humidity_2m", 0))
        wind = round(cur.get("wind_speed_10m", 0))
        cond = _condition(int(cur.get("weather_code", -1)))
        return _ok(
            f"{_label(geo)} — {temp}°F, {cond}\n"
            f"feels like {feels}°F, humidity {humidity}%, wind {wind}mph"
        )

    @tool(
        "weather_forecast",
        (
            "Get a multi-day daily forecast (highs/lows, conditions, "
            "precipitation chance) for a place. Defaults to 5 days; max 16."
        ),
        {
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 16,
                    "description": "Number of days. Default 5.",
                },
            },
            "required": ["location"],
        },
    )
    async def weather_forecast(args: dict[str, Any]) -> dict[str, Any]:
        days = int(args.get("days", 5))
        try:
            geo = _geocode(args["location"])
        except requests.RequestException as e:
            return _err(f"weather geocode failed: {e}")
        if not geo:
            return _err(f"couldn't find location: {args['location']!r}")

        try:
            resp = requests.get(
                FORECAST_URL,
                params={
                    "latitude": geo["latitude"],
                    "longitude": geo["longitude"],
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max",
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "precipitation_unit": "inch",
                    "timezone": "auto",
                    "forecast_days": days,
                },
                timeout=TIMEOUT_S,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            return _err(f"weather forecast failed: {e}")

        daily = (resp.json() or {}).get("daily") or {}
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])
        precips = daily.get("precipitation_probability_max", [])
        if not dates:
            return _err("no forecast data returned")

        lines = [f"{days}-day forecast for {_label(geo)}:"]
        for i, date in enumerate(dates):
            high = round(highs[i]) if i < len(highs) else "?"
            low = round(lows[i]) if i < len(lows) else "?"
            cond = _condition(int(codes[i])) if i < len(codes) else "?"
            precip = precips[i] if i < len(precips) else 0
            precip_str = f", {precip}% rain" if precip and precip > 0 else ""
            lines.append(f"- {date}: {high}°/{low}°, {cond}{precip_str}")
        return _ok("\n".join(lines))

    return create_sdk_mcp_server(
        name="weather",
        version="1.0.0",
        tools=[weather_current, weather_forecast],
    )


def main() -> None:
    raise NotImplementedError(
        "weather_server is in-process; instantiate via create_weather_mcp_server() from agent_host."
    )


if __name__ == "__main__":
    main()
