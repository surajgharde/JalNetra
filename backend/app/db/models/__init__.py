"""ORM models. Each section adds its models here so Alembic sees them.

S1: water_bodies, zones. S2: scenes, scene_ingestions. S3: water_masks, raster_chips.
S4: indicator_observations (hypertable), indicator_runs. S5: baselines, rainfall
(plus the indicator_weekly continuous aggregate, which has no ORM model).
S6: anomaly_candidates, anomaly_runs. S7: candidate_scores, priority_models.
S8: alerts, recipients, dispatches. S9: jobs, validations.
"""

from app.db.base import Base
from app.db.models.alerts import Alert, Dispatch, Recipient
from app.db.models.anomaly import AnomalyCandidate, AnomalyRun
from app.db.models.baseline import Baseline, Rainfall
from app.db.models.indicators import IndicatorObservation, IndicatorRun
from app.db.models.jobs import Job, Validation
from app.db.models.masks import RasterChip, WaterMaskRecord
from app.db.models.registry import WaterBody, Zone
from app.db.models.scenes import Scene, SceneIngestion
from app.db.models.scoring import CandidateScore, PriorityModel

__all__ = [
    "Alert",
    "AnomalyCandidate",
    "AnomalyRun",
    "Base",
    "Baseline",
    "CandidateScore",
    "Dispatch",
    "IndicatorObservation",
    "IndicatorRun",
    "Job",
    "PriorityModel",
    "Rainfall",
    "RasterChip",
    "Recipient",
    "Scene",
    "SceneIngestion",
    "Validation",
    "WaterBody",
    "WaterMaskRecord",
    "Zone",
]
