"""Geographic search & water body discovery (S14): resolve a place name, scan
around it for water bodies, and register whichever one the user picks."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SearchAndDiscoverRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="a place or water body name")
    # The real min/max/default live in Settings (discovery_min_radius_km etc.) so
    # an operator can retune them without a schema change; this is only a sanity
    # ceiling against an obviously bad value, enforced against the live setting
    # in the endpoint the same way gee_live_max_bbox_deg is.
    radius_km: Annotated[float | None, Field(gt=0, le=500)] = Field(
        default=None, description="defaults to Settings.discovery_default_radius_km when omitted"
    )

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"query": "Nagpur", "radius_km": 20}]}
    )


class DiscoveredWaterBodyOut(BaseModel):
    osm_id: str
    name: str
    kind: Literal["reservoir", "lake", "river_stretch"]
    osm_water_tag: str | None
    area_km2: float
    suggested_tier: int
    centroid: list[float] = Field(description="[lon, lat]")
    bbox: list[float] = Field(description="[minlon, minlat, maxlon, maxlat]")
    distance_km: float = Field(description="distance from the search centre")
    geometry: dict[str, Any] = Field(description="GeoJSON polygon/multipolygon, for a map preview")
    already_registered_id: str | None = Field(
        default=None, description="id of the existing water body this overlaps, if any"
    )


class SearchAndDiscoverResponse(BaseModel):
    query: str
    resolved_place: str
    centre: list[float] = Field(description="[lon, lat] the query resolved to")
    district: str | None = Field(
        description="best-effort district guess; the loader still needs one"
    )
    state: str | None
    radius_km: float
    items: list[DiscoveredWaterBodyOut]
    total: int

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "Nagpur",
                    "resolved_place": "Nagpur, Maharashtra, India",
                    "centre": [79.0882, 21.1458],
                    "district": "Nagpur",
                    "state": "Maharashtra",
                    "radius_km": 20,
                    "items": [
                        {
                            "osm_id": "way/123456",
                            "name": "Ambazari Lake",
                            "kind": "lake",
                            "osm_water_tag": "lake",
                            "area_km2": 1.52,
                            "suggested_tier": 2,
                            "centroid": [79.0417, 21.1312],
                            "bbox": [79.023, 21.123, 79.046, 21.139],
                            "distance_km": 3.1,
                            "geometry": {"type": "Polygon", "coordinates": []},
                            "already_registered_id": None,
                        }
                    ],
                    "total": 1,
                }
            ]
        }
    )


class PlaceSuggestionOut(BaseModel):
    """One live-autocomplete match, as the caller flies the map to it directly
    rather than re-geocoding the display text (which could resolve differently
    a second later)."""

    lat: float
    lon: float
    display_name: str
    district: str | None
    state: str | None


class PlaceSuggestResponse(BaseModel):
    query: str
    items: list[PlaceSuggestionOut]

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "query": "bho",
                    "items": [
                        {
                            "lat": 23.2599,
                            "lon": 77.4126,
                            "display_name": "Bhopal, Madhya Pradesh, India",
                            "district": "Bhopal",
                            "state": "Madhya Pradesh",
                        }
                    ],
                }
            ]
        }
    )


class DiscoverAtPointRequest(BaseModel):
    """Scan directly around a coordinate the caller already resolved -- e.g.
    from :class:`PlaceSuggestionOut` -- skipping Nominatim entirely."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_km: Annotated[float | None, Field(gt=0, le=500)] = Field(
        default=None, description="defaults to Settings.discovery_default_radius_km when omitted"
    )


class DiscoverAtPointResponse(BaseModel):
    centre: list[float] = Field(description="[lon, lat]")
    radius_km: float
    items: list[DiscoveredWaterBodyOut]
    total: int


class ImportDynamicRequest(BaseModel):
    """The exact discovered water body the user selected, re-posted so the
    import uses what they saw rather than re-querying Overpass (which could
    answer differently a second later)."""

    osm_id: str
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["reservoir", "lake", "river_stretch"]
    geometry: dict[str, Any] = Field(description="GeoJSON polygon/multipolygon from the scan")
    district: str = Field(min_length=1, max_length=120)
    water_body_id: str | None = Field(
        default=None, description="override the derived id, e.g. wb_ambazari_lake"
    )
    tier: Annotated[int | None, Field(ge=1, le=3)] = None
    source: str = "osm"

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "osm_id": "way/123456",
                    "name": "Ambazari Lake",
                    "kind": "lake",
                    "geometry": {"type": "Polygon", "coordinates": []},
                    "district": "Nagpur",
                    "tier": None,
                    "source": "osm",
                }
            ]
        }
    )


class ImportDynamicResponse(BaseModel):
    water_body_id: str
    created: bool = Field(description="false when this updated an already-imported water body")
    tier: int
    area_km2: float
    mgrs_tiles: list[str]
    n_zones: int
