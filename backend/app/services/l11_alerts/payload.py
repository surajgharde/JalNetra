"""Alert rows -> frozen-contract payloads (S8). Pure: takes loaded ORM objects
so the sync worker (brief, webhook body) and the async API build the exact
same JSON."""

from __future__ import annotations

from typing import Any

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping

from app.db.models import Alert, WaterBody, Zone
from app.schemas.alerts import (
    AlertContext,
    AlertList,
    AlertListItem,
    AlertOut,
    ContributionOut,
    Evidence,
    Explanation,
    GeoJSONFeature,
    GeoJSONFeatureCollection,
    IndicatorReading,
    TimelineEntry,
    WaterBodyRef,
    ZoneRef,
)
from app.services.l10_explain.explain import DISCLAIMER


def zone_ref(zone: Zone) -> ZoneRef:
    c = to_shape(zone.geom).representative_point()
    return ZoneRef(id=zone.id, name=zone.name, centroid=[round(c.x, 6), round(c.y, 6)])


def water_body_ref(wb: WaterBody) -> WaterBodyRef:
    return WaterBodyRef(id=wb.id, name=wb.name, district=wb.district)


def alert_out(alert: Alert, wb: WaterBody, zone: Zone) -> AlertOut:
    ctx = alert.context or {}
    ev = alert.evidence or {}
    return AlertOut(
        alert_id=alert.id,
        water_body=water_body_ref(wb),
        zone=zone_ref(zone),
        observed_on=alert.last_observed_at.date(),
        affected_area_km2=alert.affected_area_km2,
        primary_indicator=alert.primary_indicator,
        severity=alert.severity,
        confidence=alert.confidence,
        priority_score=alert.priority_score,
        status=alert.status,
        indicators=[IndicatorReading(**row) for row in alert.indicators],
        explanation=Explanation(
            summary=alert.summary,
            contributions=[ContributionOut(**c) for c in alert.contributions],
        ),
        context=AlertContext(
            rainfall_72h_mm=ctx.get("rainfall_72h_mm"),
            cloud_cover_pct=ctx.get("cloud_cover_pct"),
            natural_cause_likely=bool(ctx.get("natural_cause_likely", alert.natural_cause_likely)),
            rainfall_percentile=ctx.get("rainfall_percentile"),
            gate_reason=ctx.get("gate_reason"),
            baseline_status=ctx.get("baseline_status"),
            votes=ctx.get("votes"),
        ),
        evidence=Evidence(
            baseline_composite_url=ev.get("baseline_composite_url"),
            current_observation_url=ev.get("current_observation_url"),
            anomaly_mask_url=ev.get("anomaly_mask_url"),
            current_scene_id=ev.get("current_scene_id"),
            reference_scene_id=ev.get("reference_scene_id"),
            reference_observed_on=ev.get("reference_observed_on"),
            brief_url=ev.get("brief_url"),
        ),
        disclaimer=DISCLAIMER,
        first_observed_on=alert.first_observed_at.date(),
        n_observations=alert.n_observations,
        peak_priority_score=alert.peak_priority_score,
        peak_severity=alert.peak_severity,
        model_version=alert.model_version,
        timeline=[TimelineEntry(**e) for e in alert.timeline],
        updated_at=alert.updated_at,
    )


def alert_list_item(alert: Alert, wb: WaterBody, zone: Zone) -> AlertListItem:
    return AlertListItem(
        alert_id=alert.id,
        water_body=water_body_ref(wb),
        zone=zone_ref(zone),
        observed_on=alert.last_observed_at.date(),
        primary_indicator=alert.primary_indicator,
        severity=alert.severity,
        confidence=alert.confidence,
        priority_score=alert.priority_score,
        status=alert.status,
        natural_cause_likely=alert.natural_cause_likely,
        affected_area_km2=alert.affected_area_km2,
        summary=alert.summary,
        n_observations=alert.n_observations,
        disclaimer=DISCLAIMER,
    )


def alert_list(rows: list[tuple[Alert, WaterBody, Zone]], total: int) -> AlertList:
    return AlertList(
        items=[alert_list_item(a, wb, z) for a, wb, z in rows], total=total, disclaimer=DISCLAIMER
    )


def feature_properties(alert: Alert, wb: WaterBody, zone: Zone) -> dict[str, Any]:
    """Flat properties for map styling: priority_score drives colour/size."""
    c = to_shape(zone.geom).representative_point()
    return {
        "alert_id": alert.id,
        "water_body_id": wb.id,
        "water_body_name": wb.name,
        "district": wb.district,
        "zone_id": zone.id,
        "zone_name": zone.name,
        "centroid": [round(c.x, 6), round(c.y, 6)],
        "observed_on": alert.last_observed_at.date().isoformat(),
        "primary_indicator": alert.primary_indicator,
        "severity": alert.severity,
        "confidence": alert.confidence,
        "priority_score": alert.priority_score,
        "status": alert.status,
        "natural_cause_likely": alert.natural_cause_likely,
        "affected_area_km2": alert.affected_area_km2,
        "n_observations": alert.n_observations,
        "summary": alert.summary,
        "disclaimer": DISCLAIMER,
    }


def alert_feature(alert: Alert, wb: WaterBody, zone: Zone) -> GeoJSONFeature:
    return GeoJSONFeature(
        id=alert.id,
        geometry=mapping(to_shape(alert.geom)),
        properties=feature_properties(alert, wb, zone),
    )


def alerts_feature_collection(
    rows: list[tuple[Alert, WaterBody, Zone]],
) -> GeoJSONFeatureCollection:
    return GeoJSONFeatureCollection(
        features=[alert_feature(a, wb, z) for a, wb, z in rows], disclaimer=DISCLAIMER
    )
