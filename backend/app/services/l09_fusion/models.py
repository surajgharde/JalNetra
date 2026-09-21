"""Priority models (S7, L9): a transparent weighted model that is always
available, and an XGBoost regressor trained on validated outcomes once enough
labels exist. Both expose the same ``PriorityModel`` interface and the same
per-feature attribution, so L10 never branches on the model kind.

Attribution contract (used by L10):

    score = base + sum(contributions)

For the weighted model ``base`` is 0 and contribution_i = w_i * norm_i. For
XGBoost, ``base`` is the SHAP expected value and contributions are the SHAP
values of the single prediction.

## Weighted model v1

Points per fully-saturated feature (score is clipped to 0..100):

    z_ndti_turbidity        +25   turbidity z / 5 (z >= 5 is a full deviation)
    z_ndci_chlorophyll      +20   chlorophyll z / 5
    z_fai_algal             +18   floating-algae z / 5
    z_sediment_proxy        +14   sediment z / 5
    affected_area_ratio     +15   sqrt(cluster area / zone area)
    n_anomalous             +15   indicators flagged together: 1 -> 0.5, 2 -> 0.8, 3+ -> 1
    iforest_score           +10   IsolationForest outlier score, rescaled
    spatial_expansion        +5   growth vs previous pass
    zone_area_ratio          +2   zone share of the water body
    rainfall_percentile     -15   only above the seasonal median: (pct - 0.5) / 0.5
    valid_pixel_pct           0   deliberately: cloud drives *confidence*, not severity

A lone 8-sigma turbidity spike with nothing else lands at ~33 (low); the
forest agreeing lifts it past 40 (medium). Turbidity plus sediment over 40 %
of the zone with the forest agreeing reaches ~72 (high). Rain at the seasonal
95th percentile takes ~13 points away, which is what turns a monsoon runoff
spike from high into medium before the S6 cap even applies.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.storage import ObjectStore
from app.db.models import PriorityModel as PriorityModelRow
from app.services.l09_fusion.features import FEATURE_NAMES, FeatureVector

log = logging.getLogger(__name__)

WEIGHTED_VERSION = "weighted-v1"
WEIGHTS_V1: dict[str, float] = {
    "z_ndti_turbidity": 25.0,
    "z_ndci_chlorophyll": 20.0,
    "z_fai_algal": 18.0,
    "z_sediment_proxy": 14.0,
    "affected_area_ratio": 15.0,
    "n_anomalous": 15.0,
    "iforest_score": 10.0,
    "spatial_expansion": 5.0,
    "zone_area_ratio": 2.0,
    "rainfall_percentile": -15.0,
    "valid_pixel_pct": 0.0,
}
assert set(WEIGHTS_V1) == set(FEATURE_NAMES)


@dataclass(frozen=True)
class Attribution:
    score: float  # 0..100, clipped
    base: float  # model output with no evidence
    contributions: dict[str, float]  # feature -> signed points
    model_version: str


class PriorityModel(Protocol):
    version: str
    kind: str

    def attribute(self, fv: FeatureVector) -> Attribution: ...


class WeightedModel:
    kind = "weighted"

    def __init__(self, weights: dict[str, float] | None = None, version: str = WEIGHTED_VERSION):
        self.weights = dict(weights or WEIGHTS_V1)
        self.version = version

    def attribute(self, fv: FeatureVector) -> Attribution:
        contributions = {k: self.weights[k] * fv.norm[k] for k in FEATURE_NAMES}
        raw = sum(contributions.values())
        score = float(min(max(raw, 0.0), 100.0))
        # Clipping would break "score = base + sum(contributions)", so rescale
        # one side so the identity holds on what the reader sees.
        pos = sum(v for v in contributions.values() if v > 0)
        neg = sum(v for v in contributions.values() if v < 0)
        if raw > 100.0 and pos:
            factor = (100.0 - neg) / pos
            contributions = {k: (v * factor if v > 0 else v) for k, v in contributions.items()}
        elif raw < 0.0 and neg:
            # Rain alone cannot push a zone below "nothing": the discount can
            # only cancel what the evidence put in.
            factor = pos / -neg
            contributions = {k: (v * factor if v < 0 else v) for k, v in contributions.items()}
        return Attribution(
            score=score, base=0.0, contributions=contributions, model_version=self.version
        )


class XGBoostModel:
    """Regressor over the normalised feature vector, target = validated outcome
    priority (0..100). Requires the ``ml`` extra."""

    kind = "xgboost"

    def __init__(self, booster: Any, version: str):
        self.booster = booster
        self.version = version
        self._explainer: Any | None = None

    @classmethod
    def from_json(cls, blob: bytes, version: str) -> XGBoostModel:
        import xgboost as xgb  # lazy: optional extra

        booster = xgb.Booster()
        booster.load_model(bytearray(blob))
        return cls(booster, version)

    def to_json(self) -> bytes:
        raw = self.booster.save_raw("json")
        return bytes(raw)

    def attribute(self, fv: FeatureVector) -> Attribution:
        import shap  # lazy: optional extra
        import xgboost as xgb

        x = fv.as_array().reshape(1, -1)
        if self._explainer is None:
            self._explainer = shap.TreeExplainer(self.booster)
        pred = float(self.booster.predict(xgb.DMatrix(x, feature_names=list(FEATURE_NAMES)))[0])
        shap_values = np.asarray(self._explainer.shap_values(x))[0]
        base = float(np.asarray(self._explainer.expected_value).reshape(-1)[0])
        contributions = {k: float(shap_values[i]) for i, k in enumerate(FEATURE_NAMES)}
        score = float(min(max(pred, 0.0), 100.0))
        return Attribution(
            score=score, base=base, contributions=contributions, model_version=self.version
        )


# --- registry -------------------------------------------------------------------


def ensure_weighted_registered(session: Session) -> None:
    if session.get(PriorityModelRow, WEIGHTED_VERSION) is None:
        any_active = session.execute(
            select(PriorityModelRow.version).where(PriorityModelRow.active)
        ).scalar_one_or_none()
        session.add(
            PriorityModelRow(
                version=WEIGHTED_VERSION,
                kind="weighted",
                active=any_active is None,
                n_training=0,
                metrics={},
                feature_names=list(FEATURE_NAMES),
                notes="Transparent weighted model; weights documented in l09_fusion/models.py",
                trained_at=None,
            )
        )
        session.flush()


def active_model(
    session: Session, store: ObjectStore, *, settings: Settings | None = None
) -> PriorityModel:
    """The active model, or the weighted fallback when none is active or the
    active XGBoost model cannot be loaded (extra missing, blob gone)."""
    ensure_weighted_registered(session)
    row = session.execute(
        select(PriorityModelRow).where(PriorityModelRow.active)
    ).scalar_one_or_none()
    if row is None or row.kind == "weighted":
        return WeightedModel()
    try:
        assert row.s3_key
        return XGBoostModel.from_json(store.get_bytes(row.s3_key), row.version)
    except Exception as exc:
        log.warning(
            "active priority model unavailable, using weighted fallback",
            extra={"version": row.version, "error": f"{type(exc).__name__}: {exc}"},
        )
        return WeightedModel()


def activate_model(session: Session, version: str) -> None:
    if session.get(PriorityModelRow, version) is None:
        raise LookupError(f"unknown priority model {version!r}")
    session.execute(update(PriorityModelRow).values(active=False))
    session.execute(
        update(PriorityModelRow).where(PriorityModelRow.version == version).values(active=True)
    )
    session.flush()


# --- training -------------------------------------------------------------------


@dataclass(frozen=True)
class LabelledExample:
    features: FeatureVector
    target: float  # 0..100 outcome priority derived from the validation verdict


def train_xgboost(
    session: Session,
    store: ObjectStore,
    examples: list[LabelledExample],
    *,
    settings: Settings | None = None,
    activate: bool = False,
    version: str | None = None,
    min_examples: int | None = None,
) -> PriorityModelRow:
    """Fit a regressor on validated outcomes, store it in MinIO, register it.
    Refuses below ``priority_train_min_validations`` (or ``min_examples`` when the
    caller has already split off a hold-out set) so the weighted model stays."""
    settings = settings or get_settings()
    need = settings.priority_train_min_validations if min_examples is None else min_examples
    if len(examples) < need:
        raise ValueError(f"{len(examples)} labelled examples; training needs {need}")
    import xgboost as xgb  # lazy: optional extra

    x = np.vstack([e.features.as_array() for e in examples])
    y = np.array([e.target for e in examples], dtype=np.float64)
    rng = np.random.default_rng(settings.multivariate_seed)
    idx = rng.permutation(len(y))
    n_val = max(int(0.2 * len(y)), 1)
    val_idx, train_idx = idx[:n_val], idx[n_val:]
    params = {
        "objective": "reg:squarederror",
        "max_depth": 3,
        "eta": 0.05,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "min_child_weight": 3,
        "seed": settings.multivariate_seed,
    }
    dtrain = xgb.DMatrix(x[train_idx], label=y[train_idx], feature_names=list(FEATURE_NAMES))
    dval = xgb.DMatrix(x[val_idx], label=y[val_idx], feature_names=list(FEATURE_NAMES))
    booster = xgb.train(
        params,
        dtrain,
        num_boost_round=400,
        evals=[(dval, "val")],
        early_stopping_rounds=30,
        verbose_eval=False,
    )
    pred = booster.predict(dval)
    mae = float(np.mean(np.abs(pred - y[val_idx])))
    rmse = float(np.sqrt(np.mean((pred - y[val_idx]) ** 2)))

    version = version or f"xgb-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    model = XGBoostModel(booster, version)
    key = f"{settings.priority_model_prefix}/{version}.json"
    store.put_bytes(key, model.to_json(), content_type="application/json")
    row = PriorityModelRow(
        version=version,
        kind="xgboost",
        active=False,
        s3_key=key,
        n_training=len(examples),
        metrics={
            "val_mae": round(mae, 3),
            "val_rmse": round(rmse, 3),
            "n_val": int(n_val),
            "best_iteration": int(getattr(booster, "best_iteration", 0) or 0),
            "params": params,
        },
        feature_names=list(FEATURE_NAMES),
        trained_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    if activate:
        activate_model(session, version)
    log.info("priority model trained", extra={"version": version, "n": len(examples), "mae": mae})
    return row


def load_validated_examples(session: Session) -> list[LabelledExample]:
    """Labelled examples from field/lab validations (implemented by L13; imported
    lazily because L13 depends on this module)."""
    from app.services.l13_validation.training import load_validated_examples as impl

    return impl(session)


def weights_document() -> dict[str, Any]:
    """The weighted model's weights as JSON, for the API's /models endpoint."""
    return {
        "version": WEIGHTED_VERSION,
        "kind": "weighted",
        "weights": dict(WEIGHTS_V1),
        "note": "score = clip(sum(weight * normalised_feature), 0, 100)",
        "as_json": json.dumps(WEIGHTS_V1),
    }
