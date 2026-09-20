"""ORM models. Each section adds its models here so Alembic sees them.

S1: water_bodies, zones. S2 adds scenes; S4 adds indicator_observations.
"""

from app.db.base import Base
from app.db.models.registry import WaterBody, Zone

__all__ = ["Base", "WaterBody", "Zone"]
