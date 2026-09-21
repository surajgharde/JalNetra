"""Investigation brief PDF (S8, L12). One A4 page, ReportLab + matplotlib.

Two halves so the renderer is testable without a database:

* ``collect_brief_data``  - DB + MinIO reads -> ``BriefData``
* ``render_pdf``          - ``BriefData`` -> PDF bytes (+ page count, size)

Sections, top to bottom: header (water body, zone, date, priority, severity,
confidence), the explanation paragraph, the indicator table (current vs
seasonal baseline), the evidence timeline chart, the reference-vs-current
image pair, the four contributing factors as a horizontal bar chart, and a
footer with the verbatim disclaimer and the field-sampling checklist. Every
section renders from real values; where evidence is genuinely missing the
brief says so in words rather than leaving a blank.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import matplotlib

matplotlib.use("Agg")  # headless, before pyplot
import matplotlib.pyplot as plt
import numpy as np
from geoalchemy2.shape import to_shape
from pyproj import CRS, Transformer
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from shapely.ops import transform as shp_transform
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import Alert, WaterBody, Zone
from app.services.l05_water_detection.chips import read_chip
from app.services.l06_indicators.registry import INDICATORS
from app.services.l07_baseline.service import series_with_band
from app.services.l10_explain.explain import DISCLAIMER

log = logging.getLogger(__name__)

DPI = 110
FIELD_CHECKLIST = (
    "Sample inside the flagged area and at an upstream or unaffected reference point; "
    "record GPS, time, weather and visible inflows; measure turbidity (NTU), TSS, "
    "chlorophyll-a, DO, pH, temperature and conductivity; photograph the water surface; "
    "submit results with this alert id via POST /api/v1/validations."
)


@dataclass(frozen=True)
class ChipImage:
    values: np.ndarray  # float32 with NaN off-water
    extent: tuple[float, float, float, float]  # left, right, bottom, top in chip CRS
    crs: str
    observed_on: date
    label: str


@dataclass
class BriefData:
    alert_id: str
    water_body_name: str
    district: str
    zone_name: str
    observed_on: date
    first_observed_on: date
    n_observations: int
    priority_score: float
    severity: str
    confidence: float
    natural_cause_likely: bool
    affected_area_km2: float | None
    zone_area_km2: float
    primary_indicator: str
    summary: str
    indicators: list[dict[str, Any]]
    contributions: list[dict[str, Any]]
    context: dict[str, Any]
    model_version: str
    # evidence timeline for the primary indicator
    series_points: list[dict[str, Any]] = field(default_factory=list)
    current_image: ChipImage | None = None
    reference_image: ChipImage | None = None
    outline_geom_4326: Any | None = None  # shapely, the alert polygon
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class BriefResult:
    pdf: bytes
    n_pages: int

    @property
    def size_bytes(self) -> int:
        return len(self.pdf)


# --- data -------------------------------------------------------------------------


def _chip_image(
    store: ObjectStore, key: str | None, observed_on: date | None, label: str
) -> ChipImage | None:
    if not key or observed_on is None:
        return None
    try:
        data, transform, crs = read_chip(store, key)
    except Exception as exc:
        log.warning("brief: chip unavailable", extra={"key": key, "error": str(exc)})
        return None
    h, w = data.shape
    left, top = transform * (0, 0)
    right, bottom = transform * (w, h)
    return ChipImage(
        values=np.asarray(data, dtype=np.float32),
        extent=(float(left), float(right), float(bottom), float(top)),
        crs=crs,
        observed_on=observed_on,
        label=label,
    )


def collect_brief_data(
    session: Session,
    store: ObjectStore,
    alert: Alert,
    *,
    settings: Settings | None = None,
) -> BriefData:
    settings = settings or get_settings()
    wb = session.get(WaterBody, alert.water_body_id)
    zone = session.get(Zone, alert.zone_id)
    assert wb is not None and zone is not None
    to = alert.last_observed_at.date()
    series = series_with_band(
        session,
        zone.id,
        alert.primary_indicator,
        to - timedelta(days=30 * settings.brief_series_months),
        to,
        settings=settings,
    )
    ev = alert.evidence or {}
    ref_on = ev.get("reference_observed_on")
    return BriefData(
        alert_id=alert.id,
        water_body_name=wb.name,
        district=wb.district,
        zone_name=zone.name,
        observed_on=to,
        first_observed_on=alert.first_observed_at.date(),
        n_observations=alert.n_observations,
        priority_score=alert.priority_score,
        severity=alert.severity,
        confidence=alert.confidence,
        natural_cause_likely=alert.natural_cause_likely,
        affected_area_km2=alert.affected_area_km2,
        zone_area_km2=zone.area_km2,
        primary_indicator=alert.primary_indicator,
        summary=alert.summary,
        indicators=list(alert.indicators),
        contributions=list(alert.contributions),
        context=dict(alert.context or {}),
        model_version=alert.model_version,
        series_points=list(series.get("points", [])),
        current_image=_chip_image(
            store, ev.get("current_chip_key"), to, f"Current - {to.isoformat()}"
        ),
        reference_image=_chip_image(
            store,
            ev.get("reference_chip_key"),
            date.fromisoformat(ref_on) if ref_on else None,
            f"Reference - {ref_on}" if ref_on else "Reference",
        ),
        outline_geom_4326=to_shape(alert.geom) if alert.geom is not None else None,
    )


# --- charts -----------------------------------------------------------------------


def _png(fig: Any) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


def timeline_chart(data: BriefData) -> io.BytesIO:
    ind = INDICATORS.get(data.primary_indicator)
    name = ind.display_name if ind else data.primary_indicator
    fig, ax = plt.subplots(figsize=(7.2, 1.9))
    pts = data.series_points
    if pts:
        t = np.array([datetime.fromisoformat(p["observed_at"]) for p in pts])
        v = [p["mean"] for p in pts]
        lo = [
            p["baseline"]["p10"]
            if p.get("baseline") and p["baseline"].get("p10") is not None
            else np.nan
            for p in pts
        ]
        hi = [
            p["baseline"]["p90"]
            if p.get("baseline") and p["baseline"].get("p90") is not None
            else np.nan
            for p in pts
        ]
        med = [
            p["baseline"]["mean"]
            if p.get("baseline") and p["baseline"].get("mean") is not None
            else np.nan
            for p in pts
        ]
        ax.fill_between(t, lo, hi, color="#9ecae1", alpha=0.5, label="seasonal p10-p90")
        ax.plot(t, med, color="#3182bd", lw=1, label="seasonal median")
        ax.plot(t, v, "o-", color="#252525", ms=3, lw=1, label="observed")
        last = [
            i
            for i, p in enumerate(pts)
            if datetime.fromisoformat(p["observed_at"]).date() == data.observed_on
        ]
        if last:
            ax.plot([t[last[-1]]], [v[last[-1]]], "o", color="#d62728", ms=6, label="this alert")
        ax.legend(fontsize=6, loc="upper left", ncol=4, frameon=False)
    else:
        ax.text(
            0.5, 0.5, "No observations in the evidence window", ha="center", va="center", fontsize=8
        )
    ax.set_title(f"{name}: last {len(pts)} observations vs seasonal baseline", fontsize=8)
    ax.tick_params(labelsize=6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    return _png(fig)


def contributions_chart(data: BriefData) -> io.BytesIO:
    rows = data.contributions[:4]
    fig, ax = plt.subplots(figsize=(3.4, 1.7))
    labels = [r["factor"] for r in rows][::-1]
    vals = [float(r["value"]) for r in rows][::-1]
    colours = ["#d62728" if v < 0 else "#3182bd" for v in vals]
    ax.barh(labels, vals, color=colours)
    ax.axvline(0, color="#555", lw=0.6)
    span = max((abs(v) for v in vals), default=0.1) or 0.1
    ax.set_xlim(-span * 1.4 if any(v < 0 for v in vals) else -span * 0.05, span * 1.4)
    for i, v in enumerate(vals):
        ax.text(
            v + (span * 0.04 if v >= 0 else -span * 0.04),
            i,
            f"{v:+.2f}",
            va="center",
            ha="left" if v >= 0 else "right",
            fontsize=6,
        )
    ax.set_title("Top contributing factors (score points / 100)", fontsize=8)
    ax.tick_params(labelsize=6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    return _png(fig)


def image_pair_chart(data: BriefData) -> io.BytesIO:
    fig, axes = plt.subplots(1, 2, figsize=(3.6, 1.7))
    images = [data.reference_image, data.current_image]
    finite = [im.values[np.isfinite(im.values)] for im in images if im is not None]
    vmin = vmax = None
    if finite and any(f.size for f in finite):
        allv = np.concatenate([f for f in finite if f.size])
        vmin, vmax = float(np.nanpercentile(allv, 2)), float(np.nanpercentile(allv, 98))
    for ax, im, fallback in zip(axes, images, ("Reference", "Current"), strict=True):
        ax.set_xticks([])
        ax.set_yticks([])
        if im is None:
            ax.text(
                0.5, 0.5, f"{fallback} image\nnot available", ha="center", va="center", fontsize=7
            )
            ax.set_title(fallback, fontsize=7)
            continue
        ax.imshow(
            im.values,
            extent=im.extent,
            cmap="viridis",
            vmin=vmin,
            vmax=vmax,
            interpolation="nearest",
        )
        if data.outline_geom_4326 is not None:
            fwd = Transformer.from_crs(
                CRS.from_epsg(4326), CRS.from_user_input(im.crs), always_xy=True
            )
            g = shp_transform(fwd.transform, data.outline_geom_4326)
            for poly in getattr(g, "geoms", [g]):
                x, y = poly.exterior.xy
                ax.plot(x, y, color="#d62728", lw=0.8)
        # Keep the view on the chip even if the outline strays outside it.
        ax.set_xlim(im.extent[0], im.extent[1])
        ax.set_ylim(im.extent[2], im.extent[3])
        ax.set_title(im.label, fontsize=7)
    return _png(fig)


# --- PDF ---------------------------------------------------------------------------


class _PageCounter:
    def __init__(self) -> None:
        self.pages = 0

    def __call__(self, canvas: Any, doc: Any) -> None:
        self.pages += 1
        canvas.saveState()
        canvas.setFont("Helvetica", 6)
        canvas.setFillColor(colors.grey)
        canvas.drawString(12 * mm, 6 * mm, f"JalNetra investigation brief - page {self.pages}")
        canvas.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "t", parent=base["Heading1"], fontSize=13, leading=15, spaceAfter=1
        ),
        "sub": ParagraphStyle(
            "s", parent=base["Normal"], fontSize=8, leading=10, textColor=colors.HexColor("#444444")
        ),
        "h": ParagraphStyle(
            "h", parent=base["Heading3"], fontSize=8.5, leading=10, spaceBefore=3, spaceAfter=1
        ),
        "body": ParagraphStyle(
            "b", parent=base["Normal"], fontSize=8, leading=10, alignment=TA_LEFT
        ),
        "small": ParagraphStyle(
            "sm",
            parent=base["Normal"],
            fontSize=6.5,
            leading=8,
            textColor=colors.HexColor("#333333"),
        ),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=7, leading=8.5),
    }


def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _indicator_table(data: BriefData, st: dict[str, ParagraphStyle]) -> Table:
    head = [
        "Indicator",
        "Current",
        "Baseline median",
        "Baseline sigma",
        "z",
        "Deviation",
        "Baseline",
    ]
    rows: list[list[Any]] = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in head]]
    for r in data.indicators:
        ind = INDICATORS.get(r["key"])
        name = ind.display_name if ind else r["key"]
        dev = r.get("deviation_pct")
        rows.append(
            [
                Paragraph(
                    ("<b>%s</b>" if r["key"] == data.primary_indicator else "%s") % name, st["cell"]
                ),
                _fmt(r.get("value")),
                _fmt(r.get("baseline_mean")),
                _fmt(r.get("baseline_std")),
                _fmt(r.get("z_score"), 2),
                "-" if dev is None else f"{dev:+.1f}%",
                r.get("baseline_status") or "-",
            ]
        )
    t = Table(rows, colWidths=[52 * mm, 20 * mm, 26 * mm, 24 * mm, 14 * mm, 20 * mm, 18 * mm])
    t.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        )
    )
    return t


def render_pdf(data: BriefData) -> BriefResult:
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"JalNetra investigation brief {data.alert_id}",
        author="JalNetra",
    )
    sev_colour = {"high": "#b30000", "medium": "#e6550d", "low": "#31a354"}.get(
        data.severity, "#333"
    )
    ind = INDICATORS.get(data.primary_indicator)
    ind_name = ind.display_name if ind else data.primary_indicator
    ctx = data.context
    area = (
        f"{data.affected_area_km2:.2f} km2 flagged"
        if data.affected_area_km2
        else f"whole zone ({data.zone_area_km2:.2f} km2), no distinct patch"
    )
    rain = ctx.get("rainfall_72h_mm")
    cloud = ctx.get("cloud_cover_pct")
    header_meta = (
        f"<b>{data.water_body_name}</b> ({data.district}) - <b>{data.zone_name}</b> - observed "
        f"<b>{data.observed_on.isoformat()}</b>"
        + (
            f" (first {data.first_observed_on.isoformat()}, {data.n_observations} observations)"
            if data.n_observations > 1
            else ""
        )
    )
    verdict = (
        f'<font color="{sev_colour}"><b>Severity {data.severity.upper()}</b></font> - '
        f"priority <b>{data.priority_score:.0f}/100</b> - confidence <b>{data.confidence:.2f}</b>"
        + (" - <b>natural cause likely (rainfall)</b>" if data.natural_cause_likely else "")
        + f" - {area}"
    )
    context_line = (
        f"Rainfall 72 h: {_fmt(rain, 1)} mm"
        + (
            f" (seasonal percentile {ctx['rainfall_percentile']:.2f})"
            if ctx.get("rainfall_percentile") is not None
            else ""
        )
        + f" - cloud over zone: {_fmt(cloud, 1)} % - model {data.model_version}"
        + f" - alert {data.alert_id}"
    )

    story: list[Any] = [
        Paragraph("JalNetra investigation brief", st["title"]),
        Paragraph(header_meta, st["sub"]),
        Paragraph(verdict, st["body"]),
        Paragraph(context_line, st["small"]),
        Paragraph("Why this was flagged", st["h"]),
        Paragraph(data.summary, st["body"]),
        Paragraph("Indicators: current observation vs seasonal baseline", st["h"]),
        _indicator_table(data, st),
        Paragraph("Evidence timeline", st["h"]),
        Image(timeline_chart(data), width=182 * mm, height=48 * mm),
        Spacer(1, 1 * mm),
    ]
    pair = Image(image_pair_chart(data), width=90 * mm, height=42 * mm)
    bars = Image(contributions_chart(data), width=88 * mm, height=42 * mm)
    side = Table(
        [
            [
                Paragraph(f"Reference vs current observation - {ind_name}", st["h"]),
                Paragraph("Contributing factors", st["h"]),
            ],
            [pair, bars],
        ],
        colWidths=[92 * mm, 90 * mm],
    )
    side.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])
    )
    story += [
        side,
        Spacer(1, 1 * mm),
        Paragraph("Field sampling checklist", st["h"]),
        Paragraph(FIELD_CHECKLIST, st["small"]),
        Spacer(1, 1 * mm),
        Paragraph(
            f"<b>{DISCLAIMER}</b> This brief reports a location and an observation; it does "
            f"not attribute cause or name a source. Generated {data.generated_at:%Y-%m-%d %H:%M} "
            "UTC.",
            st["small"],
        ),
    ]
    counter = _PageCounter()
    doc.build(story, onFirstPage=counter, onLaterPages=counter)
    pdf = buf.getvalue()
    if counter.pages != 1:
        log.warning("brief spilled to %d pages", counter.pages, extra={"alert_id": data.alert_id})
    return BriefResult(pdf=pdf, n_pages=counter.pages)


# --- orchestration ------------------------------------------------------------------


def brief_key(alert_id: str, settings: Settings) -> str:
    return f"{settings.brief_prefix}/{alert_id}.pdf"


def generate_brief(
    session: Session,
    store: ObjectStore,
    alert: Alert,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> tuple[str, bool]:
    """Render and store the brief unless one already covers the latest
    observation. Returns (key, did_work)."""
    settings = settings or get_settings()
    key = brief_key(alert.id, settings)
    if (
        not force
        and alert.brief_key == key
        and alert.brief_for_observation_at == alert.last_observed_at
        and store.exists(key)
    ):
        return key, False
    data = collect_brief_data(session, store, alert, settings=settings)
    result = render_pdf(data)
    store.put_bytes(key, result.pdf, content_type="application/pdf")
    alert.brief_key = key
    alert.brief_generated_at = datetime.now(UTC)
    alert.brief_for_observation_at = alert.last_observed_at
    session.flush()
    log.info(
        "brief generated",
        extra={"alert_id": alert.id, "bytes": result.size_bytes, "pages": result.n_pages},
    )
    return key, True
