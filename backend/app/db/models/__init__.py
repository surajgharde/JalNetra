"""ORM models. Each section adds its models here so Alembic sees them.

S1: water_bodies, zones. S2: scenes, scene_ingestions. S4 adds indicator_observations.
"""

from app.db.base import Base
from app.db.models.registry import WaterBody, Zone
from app.db.models.scenes import Scene, SceneIngestion

__all__ = ["Base", "Scene", "SceneIngestion", "WaterBody", "Zone"]
