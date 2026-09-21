"""ORM models. Each section adds its models here so Alembic sees them.

S1: water_bodies, zones. S2: scenes, scene_ingestions. S3: water_masks, raster_chips.
S4: indicator_observations (hypertable), indicator_runs. S5 adds baselines and rainfall.
"""

from app.db.base import Base
from app.db.models.indicators import IndicatorObservation, IndicatorRun
from app.db.models.masks import RasterChip, WaterMaskRecord
from app.db.models.registry import WaterBody, Zone
from app.db.models.scenes import Scene, SceneIngestion

__all__ = [
    "Base",
    "IndicatorObservation",
    "IndicatorRun",
    "RasterChip",
    "Scene",
    "SceneIngestion",
    "WaterBody",
    "WaterMaskRecord",
    "Zone",
]
