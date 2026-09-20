# JalNetra

Satellite-based water quality and contamination intelligence for Maharashtra.

**Product boundary.** JalNetra detects *potential anomalies* in satellite-observable
water indicators and prioritises zones for ground investigation. It never claims to
detect, prove or confirm pollution or contamination. Every alert carries a `disclaimer`.

See `JalNetra — Maharashtra State-Wide Implementation Plan.md` for the full plan.
This repository is built section by section (S0 → S12) from that plan.

## Layout

```
backend/    FastAPI app + Celery workers (Python 3.11, uv)
  app/api/        routers, middleware
  app/core/       config, logging, health probes
  app/db/         SQLAlchemy session, models, Alembic migrations
  app/services/   one package per pipeline layer L3–L13 (+ registry)
  app/workers/    Celery app, tasks, signals
  app/schemas/    Pydantic v2 response models
  tests/
frontend/   Vite + React 18 (screens land in S10)
infra/      compose init SQL
notebooks/  exploration
```

## Quick start (Docker only)

```sh
cp .env.example .env
make up          # api :8000, postgres :5432, redis :6379, minio :9000/:9001, titiler :8001
make migrate     # alembic upgrade head
make test        # pytest inside the api image
curl localhost:8000/health
```

`make test-integration` runs the same suite plus tests that hit the live stack.
Without `make` (e.g. Windows), run the commands from the `Makefile` directly, e.g.
`docker compose run --rm api alembic upgrade head`.

## Local development without Docker

```sh
cd backend
uv sync                # creates .venv with Python 3.11
uv run pytest
uv run ruff check . && uv run mypy .
uv run uvicorn app.main:app --reload
```

## Services

| Service  | Image                              | Port       |
| -------- | ---------------------------------- | ---------- |
| api      | `backend/Dockerfile` (uvicorn)     | 8000       |
| worker   | same image, `celery worker`        | —          |
| beat     | same image, `celery beat`          | —          |
| postgres | `timescale/timescaledb-ha:pg16`    | 5432       |
| redis    | `redis:7-alpine`                   | 6379       |
| minio    | `quay.io/minio/minio`                      | 9000, 9001 |
| titiler  | `ghcr.io/developmentseed/titiler`  | 8001       |

## Water body registry (S1)

```sh
make seed                                   # 30 Pune-district bodies from OSM outlines, 4-8 zones each
make load f=my_bodies.geojson d=Nashik      # any GeoJSON / shapefile; props: name, tier, kind, drinking_water, urban
make mgrs-grid                              # optional: exact Sentinel-2 tile lookup from ESA's KML grid
```

- Tiering: Tier 1 = drinking-water sources, urban river stretches, or >= 10 km²;
  Tier 2 >= 1 km²; Tier 3 the rest. An explicit `tier` property wins.
- Zones: k-means over pixel centroids → Voronoi cells clipped to the body, so
  zones tile it exactly. Zones are only regenerated when the geometry changes.
- MGRS tiles: computed arithmetically by default; `make mgrs-grid` switches to
  the exact ESA grid (cache under `backend/data/mgrs/`, git-ignored).
- Seed outlines © OpenStreetMap contributors (ODbL), fetched via Overpass.

## Satellite ingestion (S2, L3)

```sh
# one water body, one day (Celery task; also callable from Python via app.services.l03_ingestion.service)
docker compose run --rm api python -c "from app.workers.tasks import ingest_water_body as t; print(t.delay('wb_khadakwasla','2026-05-03').get())"
```

- Sources: **Earth Search** (AWS, no auth, default) with **CDSE** (OAuth2 client
  credentials) as automatic fallback — `STAC_SOURCE`, `STAC_FALLBACK`. The source
  that served each scene is recorded on `scenes.source`.
- **Windowed reads only.** Bands B03 B04 B05 B08 B11 SCL are read through
  `/vsicurl/` for the water body's bounding box (+100 m), snapped to the 20 m grid
  so every band lands on one shared 10 m grid (20 m bands bilinear, SCL nearest).
  Khadakwasla: ~7 MB and ~20 s per pass instead of ~13 GB per tile.
- Arrays are cached in MinIO at `cache/{water_body_id}/{scene_id}/bands.npz`;
  `scene_ingestions` makes the task idempotent on (water_body_id, scene_id).
- Beat: `poll_tier1_scenes` every 6 h enqueues new Tier 1 scenes on the
  `ingestion` queue; `ingest_water_body` retries transient failures with
  exponential backoff (max 5, capped at 10 min).

## Status

- [x] S0 — scaffold and infrastructure
- [x] S1 — water body registry
- [x] S2 — satellite ingestion (L3)
- [ ] S3 — preprocessing and water mask (L4 + L5)
- [ ] S4 — spectral indicators (L6)
- [ ] S5 — baseline + rainfall (L7)
- [ ] S6 — anomaly detection (L8)
- [ ] S7 — fusion, priority, explainability (L9 + L10)
- [ ] S8 — alerts and reports (L11 + L12)
- [ ] S9 — API and tile server (L2)
- [ ] S10 — frontend (L1)
- [ ] S11 — validation loop (L13)
- [ ] S12 — scale and ops
