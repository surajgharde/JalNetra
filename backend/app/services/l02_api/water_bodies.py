"""Water body, observation, indicator and series read models (S9)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    Alert,
    AnomalyRun,
    Baseline,
    CandidateScore,
    IndicatorObservation,
    IndicatorRun,
    Scene,
    SceneIngestion,
    WaterBody,
    WaterMaskRecord,
    Zone,
)
from app.db.models.alerts import OPEN_STATUSES
from app.services.l06_indicators.registry import INDICATORS
from app.services.l07_baseline.service import (
    STATUS_USABLE,
    get_baseline,
    series_with_band,
    weekly_series,
)
from app.services.l10_explain.explain import DISCLAIMER

SEVERITY_RANK = case((Alert.severity == "high", 3), (Alert.severity == "medium", 2), else_=1)
STAGE_ORDER = ("ingested", "masked", "indicators", "anomalies", "scored", "alerted")


# --- stage -------------------------------------------------------------------------


def scene_stage(session: Session, water_body_id: str, scene_id: str) -> str | None:
    """Deepest pipeline stage a scene has reached for a body."""
    if session.execute(
        select(
            exists().where(Alert.water_body_id == water_body_id, Alert.latest_scene_id == scene_id)
        )
    ).scalar_one():
        return "alerted"
    if session.execute(
        select(
            exists().where(
                CandidateScore.water_body_id == water_body_id, CandidateScore.scene_id == scene_id
            )
        )
    ).scalar_one():
        return "scored"
    if session.execute(
        select(
            exists().where(
                AnomalyRun.water_body_id == water_body_id,
                AnomalyRun.scene_id == scene_id,
                AnomalyRun.status.in_(("done", "skipped")),
            )
        )
    ).scalar_one():
        return "anomalies"
    if session.execute(
        select(
            exists().where(
                IndicatorRun.water_body_id == water_body_id,
                IndicatorRun.scene_id == scene_id,
                IndicatorRun.status.in_(("done", "skipped")),
            )
        )
    ).scalar_one():
        return "indicators"
    if session.execute(
        select(
            exists().where(
                WaterMaskRecord.water_body_id == water_body_id,
                WaterMaskRecord.scene_id == scene_id,
                WaterMaskRecord.status == "done",
            )
        )
    ).scalar_one():
        return "masked"
    if session.execute(
        select(
            exists().where(
                SceneIngestion.water_body_id == water_body_id,
                SceneIngestion.scene_id == scene_id,
                SceneIngestion.status == "done",
            )
        )
    ).scalar_one():
        return "ingested"
    return None


# --- list / detail -----------------------------------------------------------------


def _latest_mask(session: Session, water_body_id: str) -> tuple[WaterMaskRecord, Scene] | None:
    row = session.execute(
        select(WaterMaskRecord, Scene)
        .join(Scene, Scene.id == WaterMaskRecord.scene_id)
        .where(WaterMaskRecord.water_body_id == water_body_id, WaterMaskRecord.status == "done")
        .order_by(WaterMaskRecord.sensed_at.desc())
        .limit(1)
    ).first()
    return None if row is None else (row[0], row[1])


def _baseline_status(session: Session, water_body_id: str) -> str:
    usable = session.execute(
        select(
            exists().where(
                Baseline.water_body_id == water_body_id, Baseline.status == STATUS_USABLE
            )
        )
    ).scalar_one()
    if usable:
        return "usable"
    any_row = session.execute(
        select(exists().where(Baseline.water_body_id == water_body_id))
    ).scalar_one()
    return "building" if any_row else "none"


def _zone_baseline_status(session: Session, zone_id: str) -> str:
    usable = session.execute(
        select(exists().where(Baseline.zone_id == zone_id, Baseline.status == STATUS_USABLE))
    ).scalar_one()
    if usable:
        return "usable"
    return (
        "building"
        if session.execute(select(exists().where(Baseline.zone_id == zone_id))).scalar_one()
        else "none"
    )


def _open_alerts(session: Session, water_body_id: str) -> tuple[int, str | None]:
    row = session.execute(
        select(func.count(), func.max(SEVERITY_RANK)).where(
            Alert.water_body_id == water_body_id, Alert.status.in_(OPEN_STATUSES)
        )
    ).one()
    n = int(row[0] or 0)
    sev = {3: "high", 2: "medium", 1: "low"}.get(int(row[1] or 0))
    return n, sev if n else None


def body_item(session: Session, wb: WaterBody) -> dict[str, Any]:
    geom = to_shape(wb.geom)
    c = geom.representative_point()
    n_zones = session.execute(select(func.count()).where(Zone.water_body_id == wb.id)).scalar_one()
    latest = _latest_mask(session, wb.id)
    latest_obs = None
    if latest is not None:
        mask, scene = latest
        latest_obs = {
            "scene_id": scene.id,
            "observed_on": scene.sensed_at.date(),
            "cloud_pct": scene.cloud_pct,
            "usable": bool(mask.usable),
            "stage": scene_stage(session, wb.id, scene.id) or "masked",
        }
    n_open, max_sev = _open_alerts(session, wb.id)
    baseline = _baseline_status(session, wb.id)
    if latest_obs is None:
        status = "no_data"
    elif n_open and max_sev in ("high", "medium"):
        status = "alert"
    elif n_open:
        status = "watch"
    elif baseline != "usable":
        status = "baseline_building"
    else:
        status = "normal"
    return {
        "id": wb.id,
        "name": wb.name,
        "district": wb.district,
        "kind": wb.kind,
        "tier": wb.tier,
        "area_km2": wb.area_km2,
        "centroid": [round(c.x, 6), round(c.y, 6)],
        "bbox": [round(v, 6) for v in geom.bounds],
        "n_zones": int(n_zones),
        "latest_observation": latest_obs,
        "status": status,
        "open_alerts": n_open,
        "max_open_severity": max_sev,
        "baseline_status": baseline,
    }


def list_bodies(
    session: Session,
    *,
    district: str | None,
    tier: int | None,
    limit: int,
    after: list[Any] | None,
) -> tuple[list[dict[str, Any]], int, list[Any] | None]:
    """Keyset over (tier, name, id)."""
    stmt = select(WaterBody)
    if district:
        stmt = stmt.where(WaterBody.district == district)
    if tier:
        stmt = stmt.where(WaterBody.tier == tier)
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    if after:
        a_tier, a_name, a_id = after
        stmt = stmt.where(
            (WaterBody.tier > a_tier)
            | ((WaterBody.tier == a_tier) & (WaterBody.name > a_name))
            | ((WaterBody.tier == a_tier) & (WaterBody.name == a_name) & (WaterBody.id > a_id))
        )
    rows = session.scalars(
        stmt.order_by(WaterBody.tier, WaterBody.name, WaterBody.id).limit(limit + 1)
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [body_item(session, wb) for wb in rows]
    cursor = [rows[-1].tier, rows[-1].name, rows[-1].id] if more and rows else None
    return items, int(total), cursor


def body_detail(session: Session, wb: WaterBody) -> dict[str, Any]:
    item = body_item(session, wb)
    zones = session.scalars(
        select(Zone).where(Zone.water_body_id == wb.id).order_by(Zone.seq)
    ).all()
    open_by_zone: dict[str, str] = dict(
        (str(z), str(a))
        for z, a in session.execute(
            select(Alert.zone_id, func.max(Alert.id))
            .where(Alert.water_body_id == wb.id, Alert.status.in_(OPEN_STATUSES))
            .group_by(Alert.zone_id)
        ).all()
    )
    features = [
        {
            "type": "Feature",
            "id": z.id,
            "geometry": mapping(to_shape(z.geom)),
            "properties": {
                "id": z.id,
                "name": z.name,
                "seq": z.seq,
                "area_km2": z.area_km2,
                "baseline_status": _zone_baseline_status(session, z.id),
                "open_alert_id": open_by_zone.get(z.id),
            },
        }
        for z in zones
    ]
    item.update(
        {
            "mgrs_tiles": list(wb.mgrs_tiles or []),
            "source": wb.source,
            "boundary": mapping(to_shape(wb.geom)),
            "zones": {"type": "FeatureCollection", "features": features},
            "disclaimer": DISCLAIMER,
        }
    )
    return item


# --- observations ------------------------------------------------------------------


def list_observations(
    session: Session,
    water_body_id: str,
    *,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    after: list[Any] | None,
) -> tuple[list[dict[str, Any]], int, list[Any] | None]:
    """Every scene the pipeline touched for the body, newest first. Keyset over
    (sensed_at desc, scene id)."""
    stmt = (
        select(Scene, SceneIngestion, WaterMaskRecord)
        .join(SceneIngestion, SceneIngestion.scene_id == Scene.id)
        .outerjoin(
            WaterMaskRecord,
            (WaterMaskRecord.scene_id == Scene.id)
            & (WaterMaskRecord.water_body_id == water_body_id),
        )
        .where(SceneIngestion.water_body_id == water_body_id)
    )
    if date_from:
        stmt = stmt.where(
            Scene.sensed_at >= datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
        )
    if date_to:
        stmt = stmt.where(
            Scene.sensed_at
            < datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC) + timedelta(days=1)
        )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    if after:
        a_ts, a_id = datetime.fromisoformat(after[0]), after[1]
        stmt = stmt.where(
            (Scene.sensed_at < a_ts) | ((Scene.sensed_at == a_ts) & (Scene.id > a_id))
        )
    rows = session.execute(stmt.order_by(Scene.sensed_at.desc(), Scene.id).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for scene, ing, mask in rows:
        items.append(
            {
                "scene_id": scene.id,
                "observed_on": scene.sensed_at.date(),
                "sensed_at": scene.sensed_at,
                "platform": scene.platform,
                "cloud_pct": scene.cloud_pct,
                "usable": bool(mask.usable)
                if mask is not None and mask.status == "done"
                else bool(scene.usable),
                "valid_pixel_pct": None if mask is None else mask.valid_pixel_pct,
                "water_extent_km2": None if mask is None else mask.water_extent_km2,
                "stage": scene_stage(session, water_body_id, scene.id) or ing.status,
                "source": scene.source,
            }
        )
    cursor = [rows[-1][0].sensed_at.isoformat(), rows[-1][0].id] if more and rows else None
    return items, int(total), cursor


# --- indicators --------------------------------------------------------------------


def latest_indicator_scene(
    session: Session, water_body_id: str, on_or_before: date | None = None
) -> Scene | None:
    stmt = (
        select(Scene)
        .join(IndicatorRun, IndicatorRun.scene_id == Scene.id)
        .where(IndicatorRun.water_body_id == water_body_id, IndicatorRun.status == "done")
        .order_by(Scene.sensed_at.desc())
        .limit(1)
    )
    if on_or_before:
        end = datetime(
            on_or_before.year, on_or_before.month, on_or_before.day, tzinfo=UTC
        ) + timedelta(days=1)
        stmt = stmt.where(Scene.sensed_at < end)
    return session.scalars(stmt).first()


def zone_indicators(
    session: Session,
    water_body_id: str,
    requested: date,
    zone_id: str | None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    scene = latest_indicator_scene(session, water_body_id, requested)
    zones_q = select(Zone).where(Zone.water_body_id == water_body_id).order_by(Zone.seq)
    if zone_id:
        zones_q = zones_q.where(Zone.id == zone_id)
    zones = session.scalars(zones_q).all()
    out: dict[str, Any] = {
        "water_body_id": water_body_id,
        "requested_date": requested,
        "scene_id": None if scene is None else scene.id,
        "observed_on": None if scene is None else scene.sensed_at.date(),
        "zones": [],
        "cached": False,
        "disclaimer": DISCLAIMER,
    }
    if scene is None:
        out["zones"] = [
            {"zone_id": z.id, "zone_name": z.name, "indicators": [], "rejected": []} for z in zones
        ]
        return out
    run = session.execute(
        select(IndicatorRun).where(
            IndicatorRun.water_body_id == water_body_id, IndicatorRun.scene_id == scene.id
        )
    ).scalar_one()
    obs = (
        session.execute(
            select(IndicatorObservation).where(
                IndicatorObservation.water_body_id == water_body_id,
                IndicatorObservation.scene_id == scene.id,
            )
        )
        .scalars()
        .all()
    )
    by_zone: dict[str, dict[str, IndicatorObservation]] = {}
    for row in obs:
        by_zone.setdefault(row.zone_id, {})[row.indicator] = row
    for z in zones:
        rows = []
        for key, ind in INDICATORS.items():
            o = by_zone.get(z.id, {}).get(key)
            b = get_baseline(session, z.id, key, scene.sensed_at, settings=settings)
            value = None if o is None else o.mean
            z_score = None
            dev = None
            if value is not None and b.usable and b.mean is not None and b.std is not None:
                floor = settings.anomaly_sigma_floor.get(key, 0.01)
                z_score = round((value - b.mean) / max(b.std, floor), 2)
                if abs(b.mean) > 1e-6:
                    dev = round(100.0 * (value - b.mean) / abs(b.mean), 1)
            rows.append(
                {
                    "key": key,
                    "display_name": ind.display_name,
                    "value": None if value is None else round(value, 4),
                    "p90": None if o is None or o.p90 is None else round(o.p90, 4),
                    "baseline_mean": None if b.mean is None else round(b.mean, 4),
                    "baseline_std": None if b.std is None else round(b.std, 4),
                    "baseline_p10": None if b.p10 is None else round(b.p10, 4),
                    "baseline_p90": None if b.p90 is None else round(b.p90, 4),
                    "baseline_status": b.status,
                    "z_score": z_score,
                    "deviation_pct": dev,
                    "valid_pixel_pct": None if o is None else o.valid_pixel_pct,
                    "water_fraction_pct": None if o is None else o.water_fraction_pct,
                    "scientific_basis": ind.scientific_basis,
                }
            )
        out["zones"].append(
            {
                "zone_id": z.id,
                "zone_name": z.name,
                "indicators": rows,
                "rejected": [r for r in (run.rejected or []) if r.get("zone_id") == z.id],
            }
        )
    return out


# --- series ------------------------------------------------------------------------


def series(
    session: Session,
    water_body_id: str,
    zone_id: str,
    indicator: str,
    date_from: date,
    date_to: date,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    band = series_with_band(session, zone_id, indicator, date_from, date_to, settings=settings)
    floor = settings.anomaly_sigma_floor.get(indicator, 0.01)
    points = []
    for p in band["points"]:
        b = p.get("baseline") or {}
        z = None
        if (
            p.get("mean") is not None
            and b.get("status") == STATUS_USABLE
            and b.get("mean") is not None
            and b.get("std") is not None
        ):
            z = round((p["mean"] - b["mean"]) / max(b["std"], floor), 2)
        points.append(
            {
                "observed_at": p["observed_at"],
                "scene_id": p["scene_id"],
                "value": p.get("mean"),
                "p90": p.get("p90"),
                "valid_pixel_pct": p.get("valid_pixel_pct"),
                "baseline_mean": b.get("mean"),
                "baseline_p10": b.get("p10"),
                "baseline_p90": b.get("p90"),
                "baseline_status": b.get("status"),
                "z_score": z,
            }
        )
    try:
        weekly = weekly_series(session, zone_id, indicator, date_from, date_to)
    except Exception:
        weekly = []
    return {
        "water_body_id": water_body_id,
        "zone_id": zone_id,
        "indicator": indicator,
        "display_name": INDICATORS[indicator].display_name,
        "date_from": date_from,
        "date_to": date_to,
        "points": points,
        "baseline_status": band["baseline_status"],
        "baseline_usable_windows": band["baseline_usable_windows"],
        "weekly": weekly,
        "cached": False,
        "disclaimer": DISCLAIMER,
    }


def latest_scene_marker(session: Session, water_body_id: str) -> str:
    """Cache-key ingredient: the newest scene id with any indicator run; new
    data changes the key so caches expire naturally."""
    row = session.execute(
        select(IndicatorRun.scene_id)
        .where(IndicatorRun.water_body_id == water_body_id)
        .order_by(IndicatorRun.sensed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return str(row or "none")


def first_zone_id(session: Session, water_body_id: str) -> str | None:
    return session.execute(
        select(Zone.id).where(Zone.water_body_id == water_body_id).order_by(Zone.seq).limit(1)
    ).scalar_one_or_none()
