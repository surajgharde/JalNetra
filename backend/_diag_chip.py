"""Do the hardcoded tile `rescale` ranges match what is actually in the chips?

A colormap stretched over the wrong interval renders every water pixel at the
same end of the ramp -- a flat wash over the whole body, which is exactly what
the screenshots show.
"""
import numpy as np

from app.api.tiles import LAYER_STYLES
from app.core.storage import get_store
from app.services.l05_water_detection.chips import read_chip

BODIES = ["wb_ambazari_lake", "wb_khadakwasla"]
LAYERS = ["ndti_turbidity", "ndci_chlorophyll", "fai_algal", "sediment_proxy", "mndwi_extent"]

store = get_store()

from sqlalchemy import select

from app.db.models import IndicatorRun, Scene
from app.db.sync_session import sync_session

with sync_session() as ses:
    for wb in BODIES:
        row = ses.execute(
            select(IndicatorRun.chips, Scene.sensed_at)
            .join(Scene, Scene.id == IndicatorRun.scene_id)
            .where(IndicatorRun.water_body_id == wb)
            .order_by(Scene.sensed_at.desc())
            .limit(1)
        ).first()
        if row is None:
            print(f"{wb}: no indicator runs")
            continue
        chips, sensed = row
        print("=" * 100)
        print(f"{wb}   {sensed:%Y-%m-%d}")
        print(f"{'layer':<18} {'rescale (tiles.py)':<22} {'p2':>8} {'p50':>8} {'p98':>8} "
              f"{'min':>8} {'max':>8}   {'% of ramp used':>15}")
        for key in LAYERS:
            ck = chips.get(key, {}).get("chip_key")
            if not ck:
                continue
            data, _t, _c = read_chip(store, ck)
            v = data.astype(np.float32)
            v = v[np.isfinite(v)]
            if v.size == 0:
                print(f"{key:<18} all-NaN chip")
                continue
            style = LAYER_STYLES.get(key)
            rs = style.rescale if style else None
            p2, p50, p98 = (float(np.percentile(v, q)) for q in (2, 50, 98))
            lo, hi = (rs if rs else (float(v.min()), float(v.max())))
            span = hi - lo
            # What fraction of the colour ramp the real data actually occupies.
            used = (min(p98, hi) - max(p2, lo)) / span * 100 if span else 0.0
            print(f"{key:<18} {str(rs):<22} {p2:8.3f} {p50:8.3f} {p98:8.3f} "
                  f"{float(v.min()):8.3f} {float(v.max()):8.3f}   {used:14.1f}%")
