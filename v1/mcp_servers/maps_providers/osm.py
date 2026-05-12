"""OpenStreetMap provider: Nominatim (geocoding) + OSRM (routing).

No API key required. Public Nominatim has a strict 1 req/sec rate
limit; for personal use this is fine. We send a polite User-Agent
identifying the app (Nominatim's terms require it).

Endpoints:
  - https://nominatim.openstreetmap.org/search    (places/geocode)
  - https://nominatim.openstreetmap.org/reverse   (reverse geocode)
  - https://router.project-osrm.org/route/v1/driving (drive time/distance)
"""

from __future__ import annotations

import time
from typing import Any

import requests

NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
OSRM_BASE = "https://router.project-osrm.org"
USER_AGENT = "personal-agent/1.0 (https://example.invalid; personal use)"
TIMEOUT_S = 10

# Simple module-level rate limiter — Nominatim asks for ≤1 req/sec.
_LAST_NOMINATIM_CALL: float = 0.0


def _throttle() -> None:
    global _LAST_NOMINATIM_CALL
    elapsed = time.time() - _LAST_NOMINATIM_CALL
    if elapsed < 1.05:
        time.sleep(1.05 - elapsed)
    _LAST_NOMINATIM_CALL = time.time()


def _get(url: str, **params: Any) -> Any:
    r = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
        timeout=TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()


class OSMProvider:
    name = "osm"

    def search_places(
        self,
        query: str,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: int = 5000,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        _throttle()
        params: dict[str, Any] = {"q": query, "format": "json", "limit": limit}
        # If we have a center point, bias results around it via a bounding
        # box (~radius_m in each direction; rough degree conversion).
        if lat is not None and lon is not None:
            deg = radius_m / 111_320  # ~meters per degree latitude
            params["viewbox"] = f"{lon - deg},{lat + deg},{lon + deg},{lat - deg}"
            params["bounded"] = 1
        data = _get(f"{NOMINATIM_BASE}/search", **params)
        out: list[dict[str, Any]] = []
        for item in data[:limit]:
            out.append({
                "name": item.get("display_name", "").split(",")[0],
                "address": item.get("display_name", ""),
                "lat": float(item["lat"]),
                "lon": float(item["lon"]),
                "place_id": item.get("place_id"),
            })
        return out

    def drive_time(self, origin: str, destination: str) -> dict[str, Any]:
        # OSRM needs lat/lon. Geocode both ends via Nominatim, then route.
        o = self.geocode(origin)
        d = self.geocode(destination)
        if not o or not d:
            raise RuntimeError(
                "couldn't geocode origin or destination "
                f"(origin: {bool(o)}, destination: {bool(d)})"
            )
        url = f"{OSRM_BASE}/route/v1/driving/{o['lon']},{o['lat']};{d['lon']},{d['lat']}"
        data = _get(url, overview="false", alternatives="false", steps="false")
        if not data.get("routes"):
            raise RuntimeError("OSRM returned no routes")
        route = data["routes"][0]
        return {
            "distance_m": int(route["distance"]),
            "duration_s": int(route["duration"]),
            "summary": f"{o['formatted_address']} → {d['formatted_address']}",
        }

    def geocode(self, address: str) -> dict[str, Any] | None:
        _throttle()
        data = _get(f"{NOMINATIM_BASE}/search", q=address, format="json", limit=1)
        if not data:
            return None
        item = data[0]
        return {
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "formatted_address": item.get("display_name", address),
        }

    def reverse_geocode(self, lat: float, lon: float) -> dict[str, Any] | None:
        _throttle()
        data = _get(
            f"{NOMINATIM_BASE}/reverse", lat=lat, lon=lon, format="json"
        )
        if not data or "error" in data:
            return None
        return {
            "lat": float(data["lat"]),
            "lon": float(data["lon"]),
            "formatted_address": data.get("display_name", ""),
        }
