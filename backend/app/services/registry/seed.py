"""Seed the registry with the district pilot inventories.

Each district ships a curated GeoJSON under ``seed_data/``; the loader assigns
tier, MGRS tiles and zones from the geometry, so adding a district is a data
change, not a code change.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.services.registry.load import UpsertStats, load_file

SEED_DIR = Path(__file__).parent / "seed_data"

log = logging.getLogger(__name__)


def seed_district(district: str, filename: str) -> UpsertStats:
    stats = load_file(SEED_DIR / filename, district=district, source="osm")
    log.info("district water bodies seeded", extra={"district": district, "stats": str(stats)})
    return stats


def seed_pune_water_bodies() -> UpsertStats:
    return seed_district("Pune", "pune_water_bodies.geojson")


def seed_nagpur_water_bodies() -> UpsertStats:
    return seed_district("Nagpur", "nagpur_water_bodies.geojson")


if __name__ == "__main__":
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    configure_logging(get_settings().log_level)
    print(seed_pune_water_bodies())
    print(seed_nagpur_water_bodies())
