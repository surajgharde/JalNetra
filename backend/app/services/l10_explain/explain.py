"""L10 explainability (S7). Pure functions, no I/O.

Turns a model attribution into what the UI and the PDF brief show:

* exactly **four** signed contributions (score points / 100, like the API
  example) that sum to ``score - base`` exactly: the primary deviation,
  corroborating evidence, spatial extent and rainfall. Rainfall is always one
  of the four: the model actively discounting a rain-explained spike is the
  most persuasive thing this system does, and it must render.
* a one-paragraph summary in boundary-safe language. The template is

      Flagged because <indicator> is <N>x its seasonal baseline across
      <area> km2 of <zone>, with <corroborating evidence>.

  It never says "pollution", "contamination", "discharge" or names a source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.l06_indicators.registry import INDICATORS, QUALITY_INDICATOR_KEYS
from app.services.l09_fusion.features import FACTOR_LABELS, FEATURE_NAMES, FeatureVector
from app.services.l09_fusion.models import Attribution

N_CONTRIBUTIONS = 4
RAINFALL_KEY = "rainfall_percentile"

DISCLAIMER = "Satellite-observed anomaly. Ground and laboratory testing recommended for validation."

# Plain words for the summary sentence; display_name stays for tables.
INDICATOR_NOUN: dict[str, str] = {
    "ndti_turbidity": "the turbidity indicator",
    "ndci_chlorophyll": "the chlorophyll indicator",
    "fai_algal": "the floating-algae indicator",
    "sediment_proxy": "the suspended-sediment indicator",
    "mndwi_extent": "the water-extent indicator",
}
CORROBORATION_NOUN: dict[str, str] = {
    "ndti_turbidity": "a correlated rise in turbidity",
    "ndci_chlorophyll": "a correlated rise in chlorophyll",
    "fai_algal": "a correlated rise in floating algae",
    "sediment_proxy": "a correlated rise in suspended sediment",
}

FORBIDDEN_WORDS = ("pollut", "contamin", "discharge", "effluent", "sewage", "illegal")


@dataclass(frozen=True)
class Contribution:
    key: str
    factor: str
    value: float  # score points / 100, signed
    raw: float | None  # the physical feature value behind a single-feature factor
    parts: dict[str, float] = field(default_factory=dict)  # feature -> points/100, for drill-down

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "factor": self.factor,
            "value": round(self.value, 3),
            "raw": None if self.raw is None else round(self.raw, 4),
            "parts": self.parts,
        }


def select_contributions(attribution: Attribution, fv: FeatureVector) -> list[Contribution]:
    """Exactly four signed contributions that sum to (score - base) exactly.

    Features are grouped into four factors rather than top-N picked, so no
    "other" bucket ever dominates the panel and rainfall always has its bar:

    1. the primary deviation  - the indicator z with the largest contribution
    2. corroborating evidence - the other indicator z's, the count of indicators
                                deviating together and the forest score
    3. spatial extent         - cluster area, its growth, the zone's share
    4. rainfall               - the seasonal-rank discount (negative or zero)
    """
    points = {k: v / 100.0 for k, v in attribution.contributions.items()}
    z_keys = [k for k in FEATURE_NAMES if k.startswith("z_")]
    primary = max(z_keys, key=lambda k: points[k])
    groups: dict[str, tuple[str, list[str]]] = {
        "primary_deviation": (FACTOR_LABELS[primary], [primary]),
        "corroboration": (
            "Corroborating indicators and their combination",
            [k for k in z_keys if k != primary] + ["n_anomalous", "iforest_score"],
        ),
        "spatial_extent": (
            "Spatial extent of affected pixels",
            ["affected_area_ratio", "spatial_expansion", "zone_area_ratio", "valid_pixel_pct"],
        ),
        RAINFALL_KEY: (FACTOR_LABELS[RAINFALL_KEY], [RAINFALL_KEY]),
    }
    out: list[Contribution] = []
    for key, (label, members) in groups.items():
        value = sum(points[m] for m in members)
        raw = fv.raw.get(members[0]) if len(members) == 1 else None
        out.append(
            Contribution(
                key,
                label,
                value,
                raw,
                parts={m: round(points[m], 4) for m in members if abs(points[m]) > 1e-9},
            )
        )
    # Reading order: the primary deviation the summary leads with, then the rest
    # by magnitude, negatives last.
    out.sort(key=lambda c: (c.key != "primary_deviation", c.value < 0, -abs(c.value)))
    return out


def _fmt_area(area_km2: float) -> str:
    return f"{area_km2:.2f}"


def multiple_phrase(value: float | None, baseline_median: float | None, z: float | None) -> str:
    """ "2.4x its seasonal baseline" when the ratio is meaningful, otherwise the z."""
    if value is not None and baseline_median is not None and baseline_median > 1e-3 and value > 0:
        return f"{value / baseline_median:.1f}x its seasonal baseline"
    if z is not None:
        side = "above" if z >= 0 else "below"
        return f"{abs(z):.1f} standard deviations {side} its seasonal baseline"
    return "outside its seasonal baseline"


def build_summary(
    *,
    flagged: bool,
    primary_indicator: str | None,
    primary_value: float | None,
    primary_baseline_median: float | None,
    primary_z: float | None,
    affected_area_km2: float | None,
    zone_area_km2: float,
    zone_name: str,
    corroborating: list[str],
    multivariate_flagged: bool,
    spatial_flagged: bool,
    natural_cause_likely: bool,
    rainfall_72h: float | None,
    rainfall_p90: float | None,
    baseline_status: str,
) -> str:
    if not flagged or primary_indicator is None:
        if baseline_status != "usable":
            return (
                f"No verdict for {zone_name}: the seasonal baseline is still building, "
                "so deviations cannot be judged yet."
            )
        return f"No deviation from the seasonal baseline was observed in {zone_name}."

    noun = INDICATOR_NOUN.get(primary_indicator, INDICATORS[primary_indicator].display_name)
    where = (
        f"across {_fmt_area(affected_area_km2)} km2 of {zone_name}"
        if affected_area_km2
        else f"across the {_fmt_area(zone_area_km2)} km2 {zone_name}"
    )
    evidence: list[str] = [
        CORROBORATION_NOUN[k]
        for k in corroborating
        if k in CORROBORATION_NOUN and k != primary_indicator
    ]
    if spatial_flagged:
        evidence.append("a distinct patch visible in the imagery")
    if multivariate_flagged:
        evidence.append("an unusual combination of indicators for this zone")
    with_clause = f", with {_join(evidence)}" if evidence else ", with no corroborating indicator"

    how_much = multiple_phrase(primary_value, primary_baseline_median, primary_z)
    sentence = f"Flagged because {noun} is {how_much} {where}{with_clause}."
    if natural_cause_likely and rainfall_72h is not None:
        p90 = (
            f" (above the seasonal 90th percentile of {rainfall_p90:.0f} mm)"
            if rainfall_p90
            else ""
        )
        sentence += (
            f" Heavy rainfall in the preceding 72 h ({rainfall_72h:.0f} mm{p90}) makes runoff "
            "the likely cause; the severity has been capped and the rainfall factor "
            "discounts the score."
        )
    elif rainfall_72h is not None and rainfall_72h > 0:
        sentence += f" Rainfall in the preceding 72 h was {rainfall_72h:.0f} mm."
    return sentence


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def check_boundary(text: str) -> None:
    """Guard: the product boundary is enforced in code, not just in review."""
    low = text.lower()
    for w in FORBIDDEN_WORDS:
        if w in low:
            raise ValueError(f"summary breaches the product boundary: {w!r} in {text!r}")


def indicator_rows(
    values: dict[str, dict[str, Any]], temporal: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """The API's ``indicators`` array: value, baseline, z and deviation per indicator."""
    rows = []
    for key in (*QUALITY_INDICATOR_KEYS, "mndwi_extent"):
        v = values.get(key, {}).get("mean")
        t = temporal.get(key, {})
        med, sig, z = t.get("baseline_median"), t.get("baseline_sigma"), t.get("z")
        dev = None
        if v is not None and med is not None and abs(med) > 1e-6:
            dev = round(100.0 * (v - med) / abs(med), 1)
        rows.append(
            {
                "key": key,
                "value": None if v is None else round(v, 4),
                "baseline_mean": None if med is None else round(med, 4),
                "baseline_std": None if sig is None else round(sig, 4),
                "z_score": None if z is None else round(z, 2),
                "deviation_pct": dev,
                "baseline_status": t.get("baseline_status"),
            }
        )
    return rows
