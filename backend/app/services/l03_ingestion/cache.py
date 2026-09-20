"""Object-store cache of windowed band arrays, so reprocessing never refetches.

Layout: ``{prefix}/{water_body_id}/{scene_id}/bands.npz`` -- one compressed NumPy
archive holding every band plus a JSON ``meta`` entry (grid, CRS, provenance).
"""

from __future__ import annotations

import io
import json
from typing import Any

import numpy as np
from affine import Affine

from app.core.storage import ObjectStore
from app.services.l03_ingestion.reader import WindowedBands


def cache_key(prefix: str, water_body_id: str, scene_id: str) -> str:
    return f"{prefix}/{water_body_id}/{scene_id}/bands.npz"


def _meta(wb: WindowedBands) -> dict[str, Any]:
    return {
        "scene_id": wb.scene_id,
        "water_body_id": wb.water_body_id,
        "crs": wb.crs,
        "transform": list(wb.transform)[:6],
        "bounds": list(wb.bounds),
        "bands": list(wb.arrays),
        "shape": list(wb.shape),
        "bytes_read": wb.bytes_read,
        "duration_s": wb.duration_s,
    }


def save_bands(store: ObjectStore, key: str, wb: WindowedBands) -> int:
    """Write the archive; returns its compressed size in bytes."""
    buf = io.BytesIO()
    payload: dict[str, Any] = {
        "meta": np.frombuffer(json.dumps(_meta(wb)).encode("utf-8"), dtype=np.uint8),
        **wb.arrays,
    }
    np.savez_compressed(buf, **payload)
    data = buf.getvalue()
    store.put_bytes(key, data, content_type="application/x-npz")
    return len(data)


def load_bands(store: ObjectStore, key: str) -> WindowedBands:
    with np.load(io.BytesIO(store.get_bytes(key))) as npz:
        meta = json.loads(bytes(npz["meta"]).decode("utf-8"))
        arrays = {b: npz[b] for b in meta["bands"]}
    a, b, c, d, e, f = meta["transform"]
    return WindowedBands(
        scene_id=meta["scene_id"],
        water_body_id=meta["water_body_id"],
        crs=meta["crs"],
        transform=Affine(a, b, c, d, e, f),
        bounds=(
            float(meta["bounds"][0]),
            float(meta["bounds"][1]),
            float(meta["bounds"][2]),
            float(meta["bounds"][3]),
        ),
        arrays=arrays,
        bytes_read=int(meta["bytes_read"]),
        duration_s=float(meta["duration_s"]),
    )
