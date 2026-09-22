"""Pipeline-run report: PDF + CSV renderers on synthetic data, no database."""

from __future__ import annotations

import csv
import io
from datetime import date

import numpy as np

from app.services.l12_delivery.job_report import (
    CSV_COLUMNS,
    DayReport,
    RasterImage,
    ReportData,
    ZoneDay,
    false_colour_png,
    render_report_csv,
    render_report_pdf,
)


def _zone(i: int, *, flagged: bool = False) -> ZoneDay:
    return ZoneDay(
        zone_id=f"z{i}",
        zone_name=f"Zone {i}",
        area_km2=1.5 + i,
        indicators={
            "ndti_turbidity": 0.05 * i,
            "ndci_chlorophyll": -0.02,
            "fai_algal": 0.001,
            "sediment_proxy": 0.03,
            "mndwi_extent": 0.4,
        },
        valid_pixel_pct=92.0,
        water_fraction_pct=80.0,
        temporal_z={"ndti_turbidity": 3.4 if flagged else 0.4, "ndci_chlorophyll": -0.1},
        max_abs_z=3.4 if flagged else 0.4,
        anomalous=["ndti_turbidity"] if flagged else [],
        baseline_status="usable",
        severity="medium" if flagged else None,
        priority_score=61.0 if flagged else 12.0,
        confidence=0.7,
        alertable=flagged,
        suppressed_reason=None if flagged else "no_detector_fired",
        natural_cause_likely=False,
        alert_id="alt_abc123" if flagged else None,
    )


def _day(d: date, *, observed: bool = True, images: bool = True) -> DayReport:
    rng = np.random.default_rng(int(d.strftime("%j")))
    day = DayReport(
        observed_on=d,
        scene_id=f"S2A_43QCA_{d:%Y%m%d}_0_L2A",
        platform="sentinel-2a",
        source="earth-search",
        scene_cloud_pct=12.0,
        mask_status="done",
        mask_usable=observed,
        valid_pixel_pct=88.0 if observed else 20.0,
        cloud_pixel_pct=12.0,
        water_extent_km2=9.6 if observed else None,
        water_fraction_pct=71.0,
        indicator_status="done" if observed else None,
        rainfall_24h=3.2,
        rainfall_72h=14.0,
        zones=[_zone(1, flagged=True), _zone(2), _zone(3)] if observed else [],
    )
    if observed and images:
        vals = rng.normal(0.1, 0.1, (40, 50)).astype(np.float32)
        vals[:5] = np.nan
        day.satellite_png = false_colour_png(
            {
                "B08": rng.integers(200, 5000, (40, 50)).astype(np.uint16),
                "B04": rng.integers(200, 3000, (40, 50)).astype(np.uint16),
                "B03": rng.integers(200, 3000, (40, 50)).astype(np.uint16),
            },
            120,
        )
        day.satellite_label = "Sentinel-2 false colour NIR/R/G (cached bands)"
        day.watermask = RasterImage(np.where(vals > 0.05, 1.0, 0.0).astype(np.float32), "mask")
        day.rasters["ndti_turbidity"] = RasterImage(vals, "Turbidity (NDTI)")
    return day


def _data(days: list[DayReport]) -> ReportData:
    return ReportData(
        job_id="job_test",
        job_kind="ingest",
        job_status="done",
        requested_by="dashboard",
        water_body_id="wb_khadakwasla",
        water_body_name="Khadakwasla Reservoir",
        district="Pune",
        area_km2=9.88,
        tier=1,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 15),
        days=days,
        alerts=[
            {
                "alert_id": "alt_abc123",
                "zone": "Zone 1",
                "indicator": "ndti_turbidity",
                "severity": "medium",
                "priority_score": 61.0,
                "confidence": 0.7,
                "first_observed_on": "2026-05-03",
                "last_observed_on": "2026-05-08",
                "status": "open",
                "summary": "Turbidity rose well above the seasonal range in Zone 1.",
            }
        ],
        scenes_found=3,
        scenes_usable=2,
        day_pages_limit=60,
    )


def test_pdf_has_summary_plus_one_page_per_observed_day() -> None:
    data = _data(
        [
            _day(date(2026, 5, 3)),
            _day(date(2026, 5, 6), observed=False),
            _day(date(2026, 5, 8)),
        ]
    )
    result = render_report_pdf(data)
    assert result.pdf.startswith(b"%PDF")
    assert result.n_pages == 3  # summary + 2 observed days (cloudy day only in the table)
    assert len(result.pdf) > 20_000  # images embedded


def test_pdf_renders_with_no_scenes_at_all() -> None:
    result = render_report_pdf(_data([]))
    assert result.n_pages == 1


def test_pdf_caps_per_day_pages() -> None:
    days = [_day(date(2026, 5, 1 + i), images=False) for i in range(6)]
    data = _data(days)
    data.day_pages_limit = 2
    assert render_report_pdf(data).n_pages == 3


def test_csv_one_row_per_day_and_zone_with_stable_columns() -> None:
    data = _data([_day(date(2026, 5, 3)), _day(date(2026, 5, 6), observed=False)])
    rows = list(csv.DictReader(io.StringIO(render_report_csv(data))))
    assert list(rows[0].keys()) == list(CSV_COLUMNS)
    assert len(rows) == 3 + 1  # 3 zones on the observed day, one bare row for the cloudy day
    first = rows[0]
    assert first["date"] == "2026-05-03" and first["zone_name"] == "Zone 1"
    assert first["anomalous_indicators"] == "ndti_turbidity" and first["alert_id"] == "alt_abc123"
    assert first["z_ndti_turbidity"] == "3.4" and first["alertable"] == "True"
    bare = rows[-1]
    assert bare["date"] == "2026-05-06" and bare["zone_id"] == "" and bare["mask_usable"] == "False"


def test_day_helpers() -> None:
    d = _day(date(2026, 5, 3))
    assert d.observed and d.n_flagged == 1 and d.n_alertable == 1 and d.max_priority == 61.0
    # area-weighted: (0.05*2.5 + 0.10*3.5 + 0.15*4.5) / 10.5
    assert abs((d.body_mean("ndti_turbidity") or 0) - (0.125 + 0.35 + 0.675) / 10.5) < 1e-9
    assert d.status_text() == "observed"
    assert _day(date(2026, 5, 6), observed=False).status_text() == "too cloudy"
