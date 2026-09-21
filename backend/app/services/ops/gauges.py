"""Operational gauges (S12): computed from the database on a beat schedule so
Prometheus can alert on *absence* - the thing counters cannot express. The
headline rule: page if any Tier 1 water body has gone ``tier1_stale_days``
without a usable scene."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import metrics
from app.db.models import Alert, Baseline, Validation, WaterBody, WaterMaskRecord, Zone
from app.db.models.alerts import OPEN_STATUSES
from app.services.l07_baseline.service import STATUS_USABLE


def tier1_staleness(session: Session, now: datetime | None = None) -> dict[str, float | None]:
    """water_body_id -> days since its last usable masked scene (None = never)."""
    now = now or datetime.now(UTC)
    last = (
        select(
            WaterMaskRecord.water_body_id.label("wb"),
            func.max(WaterMaskRecord.sensed_at).label("last"),
        )
        .where(WaterMaskRecord.status == "done", WaterMaskRecord.usable.is_(True))
        .group_by(WaterMaskRecord.water_body_id)
        .subquery()
    )
    rows = session.execute(
        select(WaterBody.id, last.c.last)
        .outerjoin(last, last.c.wb == WaterBody.id)
        .where(WaterBody.tier == 1)
    ).all()
    return {
        str(wb): (None if ts is None else round((now - ts).total_seconds() / 86400, 2))
        for wb, ts in rows
    }


def refresh_ops_gauges(session: Session) -> dict[str, Any]:
    stale = tier1_staleness(session)
    for wb, days in stale.items():
        # A body that has never had a usable scene is maximally stale.
        metrics.tier1_days_since_usable_scene.labels(water_body_id=wb).set(
            9999.0 if days is None else days
        )

    open_by_sev: dict[str, int] = {
        str(sev): int(n)
        for sev, n in session.execute(
            select(Alert.severity, func.count())
            .where(Alert.status.in_(OPEN_STATUSES))
            .group_by(Alert.severity)
        ).all()
    }
    for sev in ("low", "medium", "high"):
        metrics.open_alerts.labels(severity=sev).set(int(open_by_sev.get(sev, 0)))

    zones_total = session.execute(select(func.count()).select_from(Zone)).scalar_one()
    zones_usable = session.execute(
        select(func.count(func.distinct(Baseline.zone_id))).where(Baseline.status == STATUS_USABLE)
    ).scalar_one()
    building = int(zones_total) - int(zones_usable)
    metrics.baseline_building_zones.set(building)

    matched = session.execute(
        select(func.count()).where(Validation.verdict == "matched")
    ).scalar_one()
    not_matched = session.execute(
        select(func.count()).where(Validation.verdict == "not_matched")
    ).scalar_one()
    precision = matched / (matched + not_matched) if (matched + not_matched) else 0.0
    metrics.validation_precision.set(precision)

    return {
        "tier1_bodies": len(stale),
        "tier1_stalest_days": max((d for d in stale.values() if d is not None), default=None),
        "tier1_never_observed": sum(1 for d in stale.values() if d is None),
        "open_alerts": {k: int(v) for k, v in open_by_sev.items()},
        "baseline_building_zones": building,
        "validation_precision": round(precision, 3),
    }
