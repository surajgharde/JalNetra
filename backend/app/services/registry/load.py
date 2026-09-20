"""Registry ingestion CLI.

    python -m app.services.registry.load <file.geojson|file.shp> [--district Pune]
        [--tier N] [--source osm] [--no-zones] [--dry-run]

Reads a GeoJSON or shapefile of water bodies, computes area, tier and the
Sentinel-2 MGRS tiles each intersects, upserts ``water_bodies`` and (re)builds
``zones``. Zones are only regenerated when the body's geometry changed, so
zone ids referenced by later observations stay stable.

Recognised feature properties (all optional): ``id``, ``name``, ``district``,
``tier``, ``kind`` (reservoir | lake | river_stretch), ``drinking_water``
(bool), ``urban`` (bool), ``source``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import shapely
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import WaterBody, Zone
from app.db.sync_session import sync_session
from app.services.registry.geo import area_km2, as_multipolygon, slugify
from app.services.registry.mgrs import TileResolver, get_resolver
from app.services.registry.zones import generate_zones

log = logging.getLogger(__name__)

TIER1_MIN_KM2 = 10.0
TIER2_MIN_KM2 = 1.0
KINDS = {"reservoir", "lake", "river_stretch"}


@dataclass
class WaterBodyRecord:
    id: str
    name: str
    district: str
    kind: str
    tier: int
    geom: MultiPolygon  # EPSG:4326
    area_km2: float
    mgrs_tiles: list[str] = field(default_factory=list)
    source: str | None = None


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    zones_built: int = 0
    skipped: int = 0


def assign_tier(
    area: float, *, kind: str = "reservoir", drinking_water: bool = False, urban: bool = False
) -> int:
    """Tier 1: major reservoirs, drinking-water sources, urban river stretches.
    Tier 2: medium reservoirs. Tier 3: the rest (monthly extent check only)."""
    if drinking_water or (urban and kind == "river_stretch") or area >= TIER1_MIN_KM2:
        return 1
    if area >= TIER2_MIN_KM2:
        return 2
    return 3


def _is_missing(v: Any) -> bool:
    """None or NaN (GeoPandas reads missing properties as NaN)."""
    return v is None or (isinstance(v, float) and v != v)


def _str(v: Any) -> str:
    return "" if _is_missing(v) else str(v).strip()


def _truthy(v: Any) -> bool:
    return _str(v).lower() in {"1", "true", "yes", "y"}


def build_records(
    gdf: gpd.GeoDataFrame,
    *,
    default_district: str | None = None,
    force_tier: int | None = None,
    source: str | None = None,
    resolver: TileResolver | None = None,
) -> list[WaterBodyRecord]:
    resolver = resolver or get_resolver()
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)

    records: list[WaterBodyRecord] = []
    for _, row in gdf.iterrows():
        props = {k: row[k] for k in gdf.columns if k != gdf.geometry.name}
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        geom = shapely.make_valid(geom)
        try:
            mp = as_multipolygon(geom)
        except ValueError:
            log.warning("skipping non-polygon feature", extra={"name": props.get("name")})
            continue

        name = _str(props.get("name")) or _str(props.get("name_en"))
        if not name:
            log.warning("skipping unnamed feature", extra={"props": str(props)[:120]})
            continue
        district = _str(props.get("district")) or _str(default_district)
        if not district:
            raise ValueError(f"feature {name!r} has no district and no --district given")

        kind = _str(props.get("kind")) or "reservoir"
        if kind not in KINDS:
            kind = "reservoir"
        area = area_km2(mp)
        tier = force_tier or _int_or_none(props.get("tier"))
        if tier is None:
            tier = assign_tier(
                area,
                kind=kind,
                drinking_water=_truthy(props.get("drinking_water")),
                urban=_truthy(props.get("urban")),
            )
        wb_id = _str(props.get("id")) or f"wb_{slugify(name)}"
        records.append(
            WaterBodyRecord(
                id=wb_id,
                name=name,
                district=district,
                kind=kind,
                tier=int(tier),
                geom=mp,
                area_km2=round(area, 4),
                mgrs_tiles=resolver.tiles_for(mp),
                source=_str(props.get("source")) or _str(source) or None,
            )
        )
    return records


def _int_or_none(v: Any) -> int | None:
    try:
        return None if _is_missing(v) else int(v)
    except (TypeError, ValueError):
        return None


def upsert_records(
    session: Session, records: list[WaterBodyRecord], *, build_zones: bool = True
) -> UpsertStats:
    stats = UpsertStats()
    for rec in records:
        existing = session.get(WaterBody, rec.id)
        geom_changed = existing is None or not to_shape(existing.geom).equals_exact(
            rec.geom, tolerance=1e-9
        )
        values = {
            "id": rec.id,
            "name": rec.name,
            "district": rec.district,
            "kind": rec.kind,
            "tier": rec.tier,
            "geom": from_shape(rec.geom, srid=4326),
            "area_km2": rec.area_km2,
            "mgrs_tiles": rec.mgrs_tiles,
            "source": rec.source,
        }
        stmt = insert(WaterBody).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[WaterBody.id],
            set_={k: v for k, v in values.items() if k != "id"},
        )
        session.execute(stmt)
        if existing is None:
            stats.inserted += 1
        else:
            stats.updated += 1

        has_zones = (
            session.execute(select(Zone.id).where(Zone.water_body_id == rec.id).limit(1)).first()
            is not None
        )
        if build_zones and (geom_changed or not has_zones):
            session.execute(delete(Zone).where(Zone.water_body_id == rec.id))
            for z in generate_zones(rec.geom):
                session.add(
                    Zone(
                        id=f"{rec.id}_z{z.seq}",
                        water_body_id=rec.id,
                        seq=z.seq,
                        name=z.name,
                        geom=from_shape(z.geom, srid=4326),
                        area_km2=round(z.area_km2, 4),
                    )
                )
                stats.zones_built += 1
        session.flush()
        log.info(
            "water body upserted",
            extra={
                "water_body_id": rec.id,
                "tier": rec.tier,
                "area_km2": rec.area_km2,
                "mgrs_tiles": rec.mgrs_tiles,
                "zones_rebuilt": geom_changed or not has_zones,
            },
        )
    return stats


def load_file(
    path: Path,
    *,
    district: str | None = None,
    tier: int | None = None,
    source: str | None = None,
    build_zones: bool = True,
    dry_run: bool = False,
) -> UpsertStats:
    gdf = gpd.read_file(path)
    records = build_records(gdf, default_district=district, force_tier=tier, source=source)
    if dry_run:
        for r in records:
            print(f"{r.id:32s} tier={r.tier} area={r.area_km2:8.3f} km2 tiles={r.mgrs_tiles}")
        return UpsertStats(skipped=len(records))
    with sync_session() as session:
        stats = upsert_records(session, records, build_zones=build_zones)
        session.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("path", type=Path, help="GeoJSON or shapefile of water bodies")
    parser.add_argument("--district", help="district for features lacking a district property")
    parser.add_argument(
        "--tier", type=int, choices=(1, 2, 3), help="force a tier for every feature"
    )
    parser.add_argument("--source", help="provenance label, e.g. osm, jrc, wris")
    parser.add_argument("--no-zones", action="store_true", help="do not (re)generate zones")
    parser.add_argument("--dry-run", action="store_true", help="print records, write nothing")
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)
    stats = load_file(
        args.path,
        district=args.district,
        tier=args.tier,
        source=args.source,
        build_zones=not args.no_zones,
        dry_run=args.dry_run,
    )
    print(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
