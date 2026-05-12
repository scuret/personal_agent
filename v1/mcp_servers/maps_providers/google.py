"""Google Maps Platform provider.

Requires GOOGLE_MAPS_API_KEY in env + a billing account on the Google
Cloud project (free $200/month credit covers personal use). Better
quality + Distance Matrix routing without the public-instance rate
limits OSM has.

APIs touched:
  - Places API: Nearby Search + Text Search for search_places
  - Geocoding API: forward + reverse geocode
  - Distance Matrix API: drive_time

Uses the official `googlemaps` Python client.
"""

from __future__ import annotations

import os
from typing import Any


class GoogleProvider:
    name = "google"

    def __init__(self) -> None:
        import googlemaps  # lazy

        key = (os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip()
        if not key:
            raise RuntimeError(
                "GoogleProvider requires GOOGLE_MAPS_API_KEY in env"
            )
        self.client = googlemaps.Client(key=key)

    def search_places(
        self,
        query: str,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: int = 5000,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if lat is not None and lon is not None:
            # "Nearby" — center + radius + query keyword
            resp = self.client.places_nearby(
                location=(lat, lon), radius=radius_m, keyword=query,
            )
        else:
            # Text Search — no center; query string drives it
            resp = self.client.places(query=query)
        results = resp.get("results", [])[:limit]
        out: list[dict[str, Any]] = []
        for r in results:
            loc = r.get("geometry", {}).get("location", {})
            out.append({
                "name": r.get("name", ""),
                "address": r.get("formatted_address") or r.get("vicinity", ""),
                "lat": loc.get("lat"),
                "lon": loc.get("lng"),
                "place_id": r.get("place_id"),
                "rating": r.get("rating"),
                "user_ratings_total": r.get("user_ratings_total"),
            })
        return out

    def drive_time(self, origin: str, destination: str) -> dict[str, Any]:
        resp = self.client.distance_matrix(
            origins=[origin],
            destinations=[destination],
            mode="driving",
            departure_time="now",
        )
        try:
            element = resp["rows"][0]["elements"][0]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Distance Matrix response shape unexpected: {e}") from e
        if element.get("status") != "OK":
            raise RuntimeError(
                f"Distance Matrix returned status {element.get('status')!r}"
            )
        return {
            "distance_m": element["distance"]["value"],
            "duration_s": element["duration"]["value"],
            "summary": (
                f"{resp.get('origin_addresses', [origin])[0]} → "
                f"{resp.get('destination_addresses', [destination])[0]}"
            ),
        }

    def geocode(self, address: str) -> dict[str, Any] | None:
        results = self.client.geocode(address)
        if not results:
            return None
        r = results[0]
        loc = r["geometry"]["location"]
        return {
            "lat": loc["lat"],
            "lon": loc["lng"],
            "formatted_address": r.get("formatted_address", address),
        }

    def reverse_geocode(self, lat: float, lon: float) -> dict[str, Any] | None:
        results = self.client.reverse_geocode((lat, lon))
        if not results:
            return None
        r = results[0]
        return {
            "lat": lat,
            "lon": lon,
            "formatted_address": r.get("formatted_address", ""),
        }
