"""DB-touching half of discovery (S14): flag OSM candidates that overlap an
already-registered water body, and import the one the user picks.

The scan itself (Nominatim + Overpass) lives in
:mod:`app.services.registry.discovery` and never opens a database session --
this module is the thin bridge back to Postgres, matching the split every
other l02_api module keeps between "compute" and "the registry".
"""

from __future__ import annotations

import logging
from typing import Any

import shapely
from geoalchemy2.functions import ST_Intersects
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import WaterBody, Zone
from app.services.registry.discovery import to_water_body_record
from app.services.registry.geo import as_multipolygon
from app.services.registry.load import upsert_records

log = logging.getLogger(__name__)

# A candidate counts as "already registered" only once its overlap with an
# existing water body is substantial -- a canal or a river mouth can legally
# touch a lake's edge without being the same water body.
OVERLAP_RATIO = 0.3


def annotate_overlaps(
    session: Session, candidates: list[tuple[str, dict[str, Any]]]
) -> dict[str, str]:
    """``{osm_id: existing water_body_id}`` for every ``(osm_id, geojson)`` pair
    that overlaps a water body already in the registry by more than
    :data:`OVERLAP_RATIO`. Deliberately not cached: it reflects live registry
    state, and it is cheap -- one indexed ``ST_Intersects`` per candidate."""
    out: dict[str, str] = {}
    for osm_id, geometry in candidates:
        candidate = as_multipolygon(shapely.make_valid(shape(geometry)))
        if candidate.is_empty:
            continue
        wkb = from_shape(candidate, srid=4326)
        rows = session.execute(
            select(WaterBody.id, WaterBody.geom).where(ST_Intersects(WaterBody.geom, wkb)).limit(5)
        ).all()
        for existing_id, existing_geom in rows:
            existing_shape = to_shape(existing_geom)
            overlap = candidate.intersection(existing_shape).area
            ratio = overlap / candidate.area if candidate.area > 0 else 0.0
            if ratio >= OVERLAP_RATIO:
                out[osm_id] = existing_id
                break
    return out


def import_discovered(
    session: Session,
    geometry: dict[str, Any],
    *,
    name: str,
    district: str,
    kind: str,
    water_body_id: str | None = None,
    tier: int | None = None,
    source: str = "osm",
) -> dict[str, object]:
    """Register a discovered water body: upsert ``water_bodies`` and (re)build
    its Voronoi zones, exactly the way the bulk loader does for a seed file."""
    record = to_water_body_record(
        geometry,
        name=name,
        district=district,
        kind=kind,
        water_body_id=water_body_id,
        tier=tier,
        source=source,
    )
    existed = session.get(WaterBody, record.id) is not None
    upsert_records(session, [record], build_zones=True)
    n_zones = session.execute(select(Zone.id).where(Zone.water_body_id == record.id)).all()
    log.info(
        "water body imported via discovery",
        extra={
            "water_body_id": record.id,
            # "created" collides with the reserved LogRecord.created timestamp
            # attribute -- logging.Logger.makeRecord raises KeyError on it.
            "newly_created": not existed,
            "tier": record.tier,
            "area_km2": record.area_km2,
        },
    )
    return {
        "water_body_id": record.id,
        "created": not existed,
        "tier": record.tier,
        "area_km2": record.area_km2,
        "mgrs_tiles": record.mgrs_tiles,
        "n_zones": len(n_zones),
    }
