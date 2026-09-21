"""S8 tests: brief rendering (one page, < 2 MB, populated), alert ids and dedup
helpers, dispatch signing / e-mail rendering, contract schema."""

from __future__ import annotations

import base64
import re
import zlib
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import numpy as np
from shapely.geometry import MultiPolygon, Polygon

from app.core.config import Settings
from app.schemas.alerts import AlertOut
from app.services.l11_alerts.assembler import _append_timeline, alert_id_for, slug
from app.services.l11_alerts.evidence import tile_url
from app.services.l12_delivery.brief import BriefData, ChipImage, render_pdf
from app.services.l12_delivery.dispatch import render_email, sign

T0 = datetime(2026, 9, 17, 5, 30, tzinfo=UTC)


def _contributions() -> list[dict[str, Any]]:
    return [
        {
            "key": "primary_deviation",
            "factor": "Turbidity deviation from baseline",
            "value": 0.25,
            "raw": 8.76,
            "parts": {},
        },
        {
            "key": "corroboration",
            "factor": "Corroborating indicators and their combination",
            "value": 0.32,
            "raw": None,
            "parts": {},
        },
        {
            "key": "spatial_extent",
            "factor": "Spatial extent of affected pixels",
            "value": 0.15,
            "raw": None,
            "parts": {},
        },
        {
            "key": "rainfall_percentile",
            "factor": "Rainfall in preceding 72h",
            "value": -0.13,
            "raw": 0.93,
            "parts": {},
        },
    ]


def _indicators() -> list[dict[str, Any]]:
    return [
        {
            "key": "ndti_turbidity",
            "value": 0.312,
            "baseline_mean": 0.128,
            "baseline_std": 0.021,
            "z_score": 8.76,
            "deviation_pct": 143.8,
            "baseline_status": "usable",
        },
        {
            "key": "ndci_chlorophyll",
            "value": 0.03,
            "baseline_mean": 0.028,
            "baseline_std": 0.01,
            "z_score": 0.2,
            "deviation_pct": 7.1,
            "baseline_status": "usable",
        },
        {
            "key": "fai_algal",
            "value": 0.001,
            "baseline_mean": 0.001,
            "baseline_std": 0.001,
            "z_score": 0.0,
            "deviation_pct": 0.0,
            "baseline_status": "usable",
        },
        {
            "key": "sediment_proxy",
            "value": 0.05,
            "baseline_mean": 0.03,
            "baseline_std": 0.005,
            "z_score": 4.1,
            "deviation_pct": 66.7,
            "baseline_status": "usable",
        },
        {
            "key": "mndwi_extent",
            "value": 0.41,
            "baseline_mean": 0.4,
            "baseline_std": 0.05,
            "z_score": 0.2,
            "deviation_pct": 2.5,
            "baseline_status": "usable",
        },
    ]


def _series() -> list[dict[str, Any]]:
    rng = np.random.default_rng(0)
    pts = []
    for i in range(70):
        t = T0 - timedelta(days=5 * (69 - i))
        base = 0.12 + 0.06 * np.sin(2 * np.pi * (t.timetuple().tm_yday - 60) / 365)
        pts.append(
            {
                "observed_at": t.isoformat(),
                "scene_id": f"S{i}",
                "mean": float(base + rng.normal(0, 0.01)) if i < 69 else 0.312,
                "p90": None,
                "valid_pixel_pct": 90.0,
                "baseline": {
                    "mean": float(base),
                    "p10": float(base - 0.03),
                    "p90": float(base + 0.03),
                },
            }
        )
    return pts


def _chip(seed: int, plume: bool) -> ChipImage:
    rng = np.random.default_rng(seed)
    v = rng.normal(0.12, 0.01, (120, 160)).astype(np.float32)
    v[:20, :] = np.nan  # land
    if plume:
        v[40:80, 30:70] += 0.2
    return ChipImage(
        values=v,
        extent=(373000.0, 374600.0, 2048800.0, 2050000.0),
        crs="EPSG:32643",
        observed_on=date(2026, 9, 17) if plume else date(2025, 9, 14),
        label="Current - 2026-09-17" if plume else "Reference - 2025-09-14",
    )


def _brief_data(**kw: Any) -> BriefData:
    data = BriefData(
        alert_id="alr_2026_0917_khadakwasla_z3",
        water_body_name="Khadakwasla Reservoir",
        district="Pune",
        zone_name="Eastern zone",
        observed_on=date(2026, 9, 17),
        first_observed_on=date(2026, 9, 12),
        n_observations=2,
        priority_score=72.0,
        severity="high",
        confidence=0.89,
        natural_cause_likely=False,
        affected_area_km2=2.47,
        zone_area_km2=6.0,
        primary_indicator="ndti_turbidity",
        summary=(
            "Flagged because the turbidity indicator is 2.4x its seasonal baseline across "
            "2.47 km2 of Eastern zone, with a correlated rise in suspended sediment and a "
            "distinct patch visible in the imagery. Rainfall in the preceding 72 h was 4 mm."
        ),
        indicators=_indicators(),
        contributions=_contributions(),
        context={"rainfall_72h_mm": 4.2, "cloud_cover_pct": 8.1, "rainfall_percentile": 0.2},
        model_version="weighted-v1",
        series_points=_series(),
        current_image=_chip(1, True),
        reference_image=_chip(2, False),
        outline_geom_4326=MultiPolygon(
            [Polygon([(73.771, 18.441), (73.774, 18.441), (73.774, 18.444), (73.771, 18.444)])]
        ),
    )
    for k, v in kw.items():
        setattr(data, k, v)
    return data


def _pdf_text(pdf: bytes) -> str:
    """Crude text extraction: every literal string drawn by a Tj/TJ operator in
    every content stream (inflated when FlateDecode'd, raw otherwise)."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\n?endstream", pdf, re.S):
        raw = m.group(1)
        if raw.endswith(b"~>"):  # ReportLab default: ASCII85 then Flate
            try:
                raw = base64.a85decode(raw, adobe=True)
            except ValueError:
                continue
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        for lit in re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*T[jJ]", raw):
            out.append(lit.replace(b"\\(", b"(").replace(b"\\)", b")").decode("latin-1", "ignore"))
    return " ".join(out)


def test_brief_is_one_page_under_2mb_and_populated() -> None:
    """Plan acceptance: one-page PDF under 2 MB, every section populated, no placeholders."""
    result = render_pdf(_brief_data())
    assert result.pdf.startswith(b"%PDF") and result.n_pages == 1
    assert result.size_bytes < 2_000_000
    text = _pdf_text(result.pdf)
    for needle in (
        "Khadakwasla Reservoir",
        "Eastern zone",
        "2026-09-17",
        "Severity HIGH",
        "72/100",
        "0.89",
        "2.47 km2",
        "Turbidity (NDTI)",
        "0.312",
        "0.128",
        "Field sampling checklist",
        "Satellite-observed anomaly",
        "weighted-v1",
    ):
        assert needle in text, needle
    for placeholder in ("TODO", "lorem", "PLACEHOLDER", "None"):
        assert placeholder not in text, placeholder
    # Three embedded charts: timeline, image pair, contributions.
    assert pdf_images(result.pdf) >= 3


def pdf_images(pdf: bytes) -> int:
    return len(re.findall(rb"/Subtype\s*/Image", pdf))


def test_brief_degrades_honestly_without_evidence() -> None:
    data = _brief_data(
        series_points=[],
        current_image=None,
        reference_image=None,
        affected_area_km2=None,
        natural_cause_likely=True,
    )
    result = render_pdf(data)
    assert result.n_pages == 1
    text = _pdf_text(result.pdf)
    assert "whole zone" in text and "natural cause likely" in text


def test_alert_id_and_slug() -> None:
    wb = SimpleNamespace(id="wb_khadakwasla")
    zone = SimpleNamespace(seq=3)
    assert alert_id_for(wb, zone, T0) == "alr_2026_0917_khadakwasla_z3"  # type: ignore[arg-type]
    assert slug("Mula-Mutha (Pune) River") == "mula_mutha_pune_river"


def test_timeline_append_dedupes_and_counts_alertable() -> None:
    alert = SimpleNamespace(timeline=[], n_observations=0)
    e1 = {"observed_at": "2026-09-12T05:30:00+00:00", "candidate_id": 1, "alertable": True}
    e2 = {"observed_at": "2026-09-17T05:30:00+00:00", "candidate_id": 2, "alertable": True}
    e3 = {"observed_at": "2026-09-22T05:30:00+00:00", "candidate_id": 3, "alertable": False}
    for e in (e2, e1, e3, e2):  # out of order and a duplicate
        _append_timeline(alert, e)  # type: ignore[arg-type]
    assert [e["candidate_id"] for e in alert.timeline] == [1, 2, 3]
    assert alert.n_observations == 2


def test_tile_url_encodes_chip_key() -> None:
    url = tile_url("chips/wb_x/2026-09-17/body/ndti_turbidity.tif")
    assert (
        url == "/tiles/chip/chips%2Fwb_x%2F2026-09-17%2Fbody%2Fndti_turbidity.tif/{z}/{x}/{y}.png"
    )
    assert tile_url(None) is None


def _alert_out() -> AlertOut:
    return AlertOut.model_validate(
        {
            "alert_id": "alr_2026_0917_khadakwasla_z3",
            "water_body": {
                "id": "wb_khadakwasla",
                "name": "Khadakwasla Reservoir",
                "district": "Pune",
            },
            "zone": {
                "id": "wb_khadakwasla_z3",
                "name": "Eastern zone",
                "centroid": [73.7712, 18.4419],
            },
            "observed_on": "2026-09-17",
            "affected_area_km2": 2.47,
            "primary_indicator": "ndti_turbidity",
            "severity": "high",
            "confidence": 0.89,
            "priority_score": 72,
            "status": "open",
            "indicators": _indicators(),
            "explanation": {"summary": "Flagged because ...", "contributions": _contributions()},
            "context": {
                "rainfall_72h_mm": 4.2,
                "cloud_cover_pct": 8.1,
                "natural_cause_likely": False,
            },
            "evidence": {
                "baseline_composite_url": "/tiles/chip/x/{z}/{x}/{y}.png",
                "current_observation_url": "/tiles/chip/y/{z}/{x}/{y}.png",
                "anomaly_mask_url": "/api/v1/alerts/alr_2026_0917_khadakwasla_z3/geometry.geojson",
            },
            "disclaimer": "Satellite-observed anomaly. Ground and laboratory testing recommended for validation.",
            "first_observed_on": "2026-09-12",
            "n_observations": 2,
            "peak_priority_score": 74,
            "peak_severity": "high",
            "model_version": "weighted-v1",
            "updated_at": "2026-09-17T06:00:00Z",
        }
    )


def test_contract_shape_matches_plan_example() -> None:
    out = _alert_out().model_dump(mode="json")
    for key in (
        "alert_id",
        "water_body",
        "zone",
        "observed_on",
        "affected_area_km2",
        "primary_indicator",
        "severity",
        "confidence",
        "priority_score",
        "status",
        "indicators",
        "explanation",
        "context",
        "evidence",
        "disclaimer",
    ):
        assert key in out, key
    assert out["zone"]["centroid"] == [73.7712, 18.4419]
    assert len(out["explanation"]["contributions"]) == 4
    assert out["explanation"]["contributions"][3]["value"] < 0
    assert out["disclaimer"].startswith("Satellite-observed anomaly")


def test_email_and_signature() -> None:
    settings = Settings(public_base_url="https://jalnetra.example.org/")
    subject, html = render_email(_alert_out(), settings=settings)
    assert subject == "[JalNetra] HIGH anomaly - Khadakwasla Reservoir, Eastern zone - 2026-09-17"
    assert (
        "https://jalnetra.example.org/api/v1/alerts/alr_2026_0917_khadakwasla_z3/brief.pdf" in html
    )
    assert "Rainfall in preceding 72h: -0.13" in html and "Satellite-observed anomaly" in html
    assert "pollut" not in html.lower()
    assert sign(b"{}", None) is None
    assert sign(b"{}", "k") == sign(b"{}", "k") and sign(b"{}", "k") != sign(b"{}", "j")
    assert sign(b"{}", "k").startswith("sha256=")  # type: ignore[union-attr]
