"""Evidence links for an alert (S8). One place defines the tile URL scheme so
the alert payload, the PDF brief and the S9 tile router agree.

``/tiles/chip/{chip_key}/{z}/{x}/{y}.png`` renders a COG chip in MinIO through
TiTiler (S9 mounts it). The chip key is URL-encoded verbatim, so any layer the
pipeline writes - water mask, indicator, future anomaly rasters - is a valid
tile source without a registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import IndicatorRun, Scene
from app.services.l07_baseline.robust import circular_doy_distance, day_of_year

TILE_TEMPLATE = "/tiles/chip/{key}/{{z}}/{{x}}/{{y}}.png"
REFERENCE_DOY_WINDOW = 45  # days either side when looking for a same-season reference scene


def tile_url(chip_key: str | None) -> str | None:
    return None if not chip_key else TILE_TEMPLATE.format(key=quote(chip_key, safe=""))


@dataclass(frozen=True)
class EvidenceLinks:
    current_scene_id: str
    current_chip_key: str | None
    mask_chip_key: str | None
    reference_scene_id: str | None
    reference_observed_on: date | None
    reference_chip_key: str | None

    def to_dict(self, alert_id: str) -> dict[str, object]:
        return {
            "baseline_composite_url": tile_url(self.reference_chip_key),
            "current_observation_url": tile_url(self.current_chip_key),
            "anomaly_mask_url": f"/api/v1/alerts/{alert_id}/geometry.geojson",
            "current_scene_id": self.current_scene_id,
            "reference_scene_id": self.reference_scene_id,
            "reference_observed_on": (
                None
                if self.reference_observed_on is None
                else self.reference_observed_on.isoformat()
            ),
            "brief_url": f"/api/v1/alerts/{alert_id}/brief.pdf",
            "current_chip_key": self.current_chip_key,
            "reference_chip_key": self.reference_chip_key,
            "mask_chip_key": self.mask_chip_key,
        }


def _doy_gap(when: datetime, doy: int) -> int:
    return int(circular_doy_distance(np.array([day_of_year(when)], dtype=np.int32), doy)[0])


def reference_scene(
    session: Session, water_body_id: str, indicator: str, current: Scene
) -> tuple[Scene, str | None] | None:
    """The comparison image: the closest same-season scene from an earlier year
    with a finished L6 run (so "what this zone normally looks like in September"),
    else the most recent earlier scene."""
    stmt = (
        select(Scene, IndicatorRun.chips)
        .join(IndicatorRun, IndicatorRun.scene_id == Scene.id)
        .where(
            IndicatorRun.water_body_id == water_body_id,
            IndicatorRun.status == "done",
            Scene.sensed_at < current.sensed_at - timedelta(days=1),
        )
        .order_by(Scene.sensed_at.desc())
    )
    rows: list[tuple[Scene, dict[str, Any]]] = [(r[0], r[1] or {}) for r in session.execute(stmt)]
    if not rows:
        return None
    doy = day_of_year(current.sensed_at)
    same_season = [
        (s, chips)
        for s, chips in rows
        if s.sensed_at.year < current.sensed_at.year
        and _doy_gap(s.sensed_at, doy) <= REFERENCE_DOY_WINDOW
    ]
    pool = same_season or rows
    best = min(
        pool,
        key=lambda r: (_doy_gap(r[0].sensed_at, doy), -r[0].sensed_at.timestamp()),
    )
    scene, chips = best
    return scene, chips.get(indicator, {}).get("chip_key")


def evidence_links(
    session: Session,
    water_body_id: str,
    indicator: str,
    scene: Scene,
    *,
    current_chips: dict[str, dict[str, object]],
    mask_chip_key: str | None,
) -> EvidenceLinks:
    ref = reference_scene(session, water_body_id, indicator, scene)
    ref_scene, ref_key = (None, None) if ref is None else ref
    current_key = current_chips.get(indicator, {}).get("chip_key")
    return EvidenceLinks(
        current_scene_id=scene.id,
        current_chip_key=None if current_key is None else str(current_key),
        mask_chip_key=mask_chip_key,
        reference_scene_id=None if ref_scene is None else ref_scene.id,
        reference_observed_on=None if ref_scene is None else _as_date(ref_scene.sensed_at),
        reference_chip_key=ref_key,
    )


def _as_date(t: datetime) -> date:
    return t.date()
