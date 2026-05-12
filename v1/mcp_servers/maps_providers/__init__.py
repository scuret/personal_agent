"""Maps/Places provider abstraction.

The maps sub-agent dispatches to one of these providers based on
whether `GOOGLE_MAPS_API_KEY` is set in env. Both implement the same
small interface so swapping is a one-line check at startup.
"""

from __future__ import annotations

from typing import Protocol


class PlacesResult(dict):
    """Loose typed dict: {name, address, lat, lon, distance_m?, place_id?}."""


class DriveTimeResult(dict):
    """{distance_m, duration_s, summary?}."""


class GeocodeResult(dict):
    """{lat, lon, formatted_address}."""


class MapsProvider(Protocol):
    name: str

    def search_places(
        self,
        query: str,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: int = 5000,
        limit: int = 10,
    ) -> list[PlacesResult]: ...

    def drive_time(self, origin: str, destination: str) -> DriveTimeResult: ...

    def geocode(self, address: str) -> GeocodeResult | None: ...

    def reverse_geocode(self, lat: float, lon: float) -> GeocodeResult | None: ...


def get_provider() -> MapsProvider:
    """Pick the active provider based on env. Lazy-imports so we don't
    pay the googlemaps SDK import cost unless it's actually configured."""
    import os

    if (os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip():
        from mcp_servers.maps_providers.google import GoogleProvider
        return GoogleProvider()
    from mcp_servers.maps_providers.osm import OSMProvider
    return OSMProvider()
