"""Resolve a place name to coordinates, then scan around it for water bodies (S14).

Two external services, both public and rate-limited, so every call is cached in
Redis by the endpoint layer:

* Nominatim (OpenStreetMap's geocoder) -- "Nagpur" or "Ambazari Lake" -> a point
  and, when available, the district/state it falls in.
* Overpass (OpenStreetMap's query API) -- every ``natural=water`` way or
  relation within a radius of that point, already assembled into real polygons
  by :mod:`app.services.registry.overpass` (the same module the bulk loader
  uses), not just centroids.

Nothing here touches the database. Turning a discovered water body into a
registered one is :func:`to_water_body_record`, which produces the same
``WaterBodyRecord`` the bulk loader (`app.services.registry.load`) upserts --
so a manually discovered lake gets Voronoi zones, MGRS tiles and a tier exactly
the way a seeded one does, through the same code path.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import httpx
import shapely
from shapely.geometry import MultiPolygon, Point, shape
from shapely.geometry.base import BaseGeometry

from app.core.config import Settings
from app.services.registry.geo import area_km2, as_multipolygon, slugify, to_utm
from app.services.registry.load import KINDS, WaterBodyRecord, assign_tier
from app.services.registry.mgrs import TileResolver, get_resolver
from app.services.registry.overpass import HEADERS, OVERPASS_URL, to_feature_collection

log = logging.getLogger(__name__)


class DiscoveryError(Exception):
    """A geocode or scan request failed in a way the caller should see as 4xx/502."""


class PlaceNotFoundError(DiscoveryError):
    """Nominatim returned no match for the query."""


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    display_name: str
    district: str | None
    state: str | None


@dataclass(frozen=True)
class DiscoveredWaterBody:
    osm_id: str
    name: str
    kind: str  # reservoir | lake | river_stretch (mapped from the OSM `water` tag)
    osm_water_tag: str | None  # the raw OSM tag, for display
    area_km2: float
    suggested_tier: int
    centroid: tuple[float, float]  # lon, lat
    bbox: tuple[float, float, float, float]  # minlon, minlat, maxlon, maxlat
    distance_km: float  # from the search centre
    geometry: dict[str, Any]  # GeoJSON, for a map preview and for re-posting to import

    @property
    def geom(self) -> MultiPolygon:
        return as_multipolygon(shapely.make_valid(shape(self.geometry)))


_NOMINATIM_USER_AGENT = "JalNetra-Water-Platform"


def _parse_hit(hit: dict[str, Any], query: str) -> GeocodeResult:
    address = hit.get("address") or {}
    # Nominatim's district-equivalent field varies by country; try the common ones
    # in order of specificity before giving up (the caller may still supply one).
    district = (
        address.get("state_district")
        or address.get("county")
        or address.get("city")
        or address.get("town")
    )
    return GeocodeResult(
        lat=float(hit["lat"]),
        lon=float(hit["lon"]),
        display_name=str(hit.get("display_name") or query),
        district=str(district) if district else None,
        state=str(address["state"]) if address.get("state") else None,
    )


async def _nominatim_search(query: str, settings: Settings, limit: int) -> list[dict[str, Any]]:
    headers = {**HEADERS, "User-Agent": _NOMINATIM_USER_AGENT}
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": str(limit),
        "addressdetails": "1",
        # Restrict to India so "Bhopal" or "Udaipur" never resolves to a
        # same-named place abroad -- every water body this platform tracks
        # is Indian, and the query is always a place or lake name, not a
        # coordinate pair.
        "countrycodes": "in",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.discovery_http_timeout_s) as client:
            r = await client.get(settings.nominatim_url, params=params, headers=headers)
            r.raise_for_status()
            data: list[dict[str, Any]] = r.json()
            return data
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"geocoding service unreachable: {exc}") from exc


async def geocode_place(query: str, settings: Settings) -> GeocodeResult:
    """Resolve a free-text place or water body name to a point.

    Raises :class:`PlaceNotFoundError` when Nominatim has no match, or
    :class:`DiscoveryError` when the service itself is unreachable.
    """
    data = await _nominatim_search(query, settings, limit=1)
    if not data:
        raise PlaceNotFoundError(query)
    return _parse_hit(data[0], query)


async def geocode_suggestions(query: str, settings: Settings, limit: int = 5) -> list[GeocodeResult]:
    """Up to ``limit`` India-only place/water-body matches for a live,
    search-as-you-type dropdown -- the multi-result sibling of
    :func:`geocode_place`. Never raises for "no match", just returns ``[]``."""
    data = await _nominatim_search(query, settings, limit=limit)
    return [_parse_hit(hit, query) for hit in data]


def _kind_of(water_tag: str | None) -> str:
    """OSM's free-text ``water`` tag -> the registry's three-way kind.

    Every candidate already matched ``natural=water`` or ``water=reservoir`` in
    the Overpass query, so there is no separate ``natural`` fallback to check."""
    tag = (water_tag or "").lower()
    if tag in ("reservoir", "basin"):
        return "reservoir"
    if tag == "river":
        return "river_stretch"
    return "lake"  # lake, pond, lagoon, oxbow, canal-fed tank, or untagged


def _build_radius_query(lat: float, lon: float, radius_km: float, timeout: int = 90) -> str:
    r_m = int(radius_km * 1000)
    around = f"around:{r_m},{lat},{lon}"
    return (
        f"[out:json][timeout:{timeout}];\n"
        "(\n"
        f'  way["natural"="water"]({around});\n'
        f'  relation["natural"="water"]({around});\n'
        f'  way["water"="reservoir"]({around});\n'
        f'  relation["water"="reservoir"]({around});\n'
        ");\n"
        "out geom;\n"
    )


# Overpass sometimes has nothing tagged `natural=water` at a searched point (a
# small town, a lake OSM hasn't mapped yet) or is itself slow/unreachable. Either
# way the user searched for a real place and should still get something to look
# at, so a placeholder box goes on the map instead of a bare "0 results".
_FALLBACK_HALF_KM = 0.5


def _fallback_body(lat: float, lon: float) -> DiscoveredWaterBody:
    half_lat_deg = _FALLBACK_HALF_KM / 111.0
    half_lon_deg = _FALLBACK_HALF_KM / (111.0 * max(math.cos(math.radians(lat)), 0.15))
    minlon, maxlon = lon - half_lon_deg, lon + half_lon_deg
    minlat, maxlat = lat - half_lat_deg, lat + half_lat_deg
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [
                [minlon, minlat],
                [maxlon, minlat],
                [maxlon, maxlat],
                [minlon, maxlat],
                [minlon, minlat],
            ]
        ],
    }
    mp = as_multipolygon(shapely.make_valid(shape(geometry)))
    area = area_km2(mp)
    return DiscoveredWaterBody(
        # Prefixed so the frontend and the importer can tell this apart from a
        # real OSM feature rather than register it as one.
        osm_id=f"placeholder:{round(lat, 5)}:{round(lon, 5)}",
        name="No mapped water body here yet — inspect this location",
        kind="lake",
        osm_water_tag=None,
        area_km2=round(area, 4),
        suggested_tier=assign_tier(area, kind="lake"),
        centroid=(round(lon, 6), round(lat, 6)),
        bbox=(round(minlon, 6), round(minlat, 6), round(maxlon, 6), round(maxlat, 6)),
        distance_km=0.0,
        geometry=geometry,
    )


async def _fetch_overpass_async(query: str, settings: Settings) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=settings.discovery_http_timeout_s) as client:
            r = await client.post(OVERPASS_URL, data={"data": query}, headers=HEADERS)
            r.raise_for_status()
            payload: dict[str, Any] = r.json()
            return payload
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"discovery service unreachable: {exc}") from exc


async def scan_water_bodies(
    lat: float, lon: float, radius_km: float, settings: Settings
) -> list[DiscoveredWaterBody]:
    """Every ``natural=water`` / ``water=reservoir`` polygon within ``radius_km``
    of (lat, lon), sorted by area descending, largest first."""
    query = _build_radius_query(lat, lon, radius_km)
    try:
        payload = await _fetch_overpass_async(query, settings)
    except DiscoveryError:
        log.warning(
            "overpass unreachable, falling back to a placeholder area",
            extra={"lat": lat, "lon": lon, "radius_km": radius_km},
        )
        return [_fallback_body(lat, lon)]
    fc = to_feature_collection(payload)

    centre = Point(lon, lat)
    _, utm_crs = to_utm(centre)
    centre_utm, _ = to_utm(centre, utm_crs)

    out: list[DiscoveredWaterBody] = []
    for feat in fc["features"]:
        geom: BaseGeometry = shapely.make_valid(shape(feat["geometry"]))
        if geom.is_empty:
            continue
        try:
            mp = as_multipolygon(geom)
        except ValueError:
            continue
        props = feat["properties"]
        name = props.get("name") or props.get("name_en") or "Unnamed water body"
        area = area_km2(mp)
        kind = _kind_of(props.get("water"))
        centroid_utm, _ = to_utm(mp.centroid, utm_crs)
        distance_km = float(centre_utm.distance(centroid_utm)) / 1000.0
        c = mp.centroid
        out.append(
            DiscoveredWaterBody(
                osm_id=props["osm_id"],
                name=str(name),
                kind=kind,
                osm_water_tag=props.get("water"),
                area_km2=round(area, 4),
                suggested_tier=assign_tier(area, kind=kind),
                centroid=(round(c.x, 6), round(c.y, 6)),
                bbox=tuple(round(v, 6) for v in mp.bounds),
                distance_km=round(distance_km, 2),
                geometry=feat["geometry"],
            )
        )
        if len(out) >= settings.discovery_max_results:
            log.warning(
                "discovery result truncated",
                extra={"lat": lat, "lon": lon, "radius_km": radius_km, "limit": len(out)},
            )
            break
    out.sort(key=lambda d: d.area_km2, reverse=True)
    if not out:
        out.append(_fallback_body(lat, lon))
    return out


def to_water_body_record(
    geometry: dict[str, Any],
    *,
    name: str,
    district: str,
    kind: str,
    water_body_id: str | None = None,
    tier: int | None = None,
    source: str = "osm",
    resolver: TileResolver | None = None,
) -> WaterBodyRecord:
    """The same ``WaterBodyRecord`` shape the bulk loader upserts, built from a
    GeoJSON geometry the caller already has -- typically re-posted from a prior
    :func:`scan_water_bodies` result, since ``ImportDynamicRequest`` carries the
    exact geometry the user was shown rather than asking Overpass again.

    Importing this way goes through :func:`app.services.registry.load.upsert_records`,
    so a discovered water body gets a tier, MGRS tiles and Voronoi zones computed
    the one way the registry already knows how -- the same path a seeded water
    body goes through.
    """
    resolver = resolver or get_resolver()
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(KINDS)}")
    geom = shapely.make_valid(shape(geometry))
    if geom.is_empty:
        raise ValueError("geometry is empty")
    mp = as_multipolygon(geom)
    area = area_km2(mp)
    final_name = name.strip() or "Unnamed water body"
    return WaterBodyRecord(
        id=water_body_id or f"wb_{slugify(final_name)}",
        name=final_name,
        district=district,
        kind=kind,
        tier=int(tier) if tier is not None else assign_tier(area, kind=kind),
        geom=mp,
        area_km2=round(area, 4),
        mgrs_tiles=resolver.tiles_for(mp),
        source=source,
    )
