"""ORM models. Each section adds its models here so Alembic sees them.

S1 adds water_bodies and zones; S2 adds scenes; S4 adds indicator_observations.
"""

from app.db.base import Base

__all__ = ["Base"]
