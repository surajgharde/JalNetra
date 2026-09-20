"""Seed the registry with the Pune-district pilot inventory (30 water bodies)."""

from __future__ import annotations

import logging
from pathlib import Path

from app.services.registry.load import UpsertStats, load_file

SEED_FILE = Path(__file__).parent / "seed_data" / "pune_water_bodies.geojson"

log = logging.getLogger(__name__)


def seed_pune_water_bodies() -> UpsertStats:
    stats = load_file(SEED_FILE, district="Pune", source="osm")
    log.info("pune water bodies seeded", extra={"stats": str(stats)})
    return stats


if __name__ == "__main__":
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    configure_logging(get_settings().log_level)
    print(seed_pune_water_bodies())
