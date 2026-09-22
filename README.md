# JalNetra

**Satellite-based water quality and contamination intelligence for Maharashtra.**

JalNetra watches registered rivers, lakes and reservoirs from Sentinel-2, turns
every cloud-free pass into optically observable water-quality indicators, compares
them with each zone's own seasonal history, and raises explained, prioritised
alerts for the places that deserve a field visit. It is an early monitoring and
decision-support system built for one workflow:

> **Monitoring → Detection → Prioritisation → Investigation support**

**Product boundary.** JalNetra detects *potential anomalies* in
satellite-observable indicators. It never claims to detect, prove or confirm
pollution or contamination, never attributes a cause, and never replaces
laboratory testing. Every alert, brief and report carries the disclaimer
*"Satellite-observed anomaly. Ground and laboratory testing recommended for
validation."* and the explanation generator rejects any sentence that names
pollution, contamination or discharge.

---

## Contents

1. [What the platform does](#what-the-platform-does)
2. [Problem statement coverage](#problem-statement-coverage)
3. [Architecture](#architecture)
4. [Feature tour](#feature-tour)
5. [Quick start](#quick-start)
6. [Google Earth Engine setup](#google-earth-engine-setup)
7. [Backfill and baselines](#backfill-and-baselines)
8. [Configuration](#configuration)
9. [API reference](#api-reference)
10. [Layer-by-layer details](#layer-by-layer-details)
11. [Frontend](#frontend)
12. [Operations and observability](#operations-and-observability)
13. [Development](#development)
14. [Status, verified results and known issues](#status-verified-results-and-known-issues)
15. [Data sources and attribution](#data-sources-and-attribution)

---

## What the platform does

For every registered water body, split into 4-8 spatial zones:

- **Finds every Sentinel-2 pass** (AWS Earth Search, Copernicus CDSE or Google
  Earth Engine, with automatic fallback) and reads only the pixels over the water
  body — about 7 MB per pass instead of a 13 GB tile.
- **Isolates the water** in each pass: cloud, shadow and cirrus masked from the
  Scene Classification Layer, then an Otsu-thresholded MNDWI mask inside the
  registered outline, so the shoreline follows draw-down, refill and floods.
- **Computes five indicators** per zone per pass — turbidity (NDTI), chlorophyll-a
  (NDCI), floating algae (FAI), suspended sediment (red reflectance) and water
  extent (MNDWI) — each with a documented formula and scientific basis.
- **Builds a seasonal baseline** per zone and indicator (robust day-of-year
  median / MAD over multi-year history) plus daily rainfall as a covariate.
- **Detects anomalies** with three independent detectors — temporal (robust z
  against the seasonal baseline), spatial (DBSCAN plumes within the scene) and
  multivariate (IsolationForest over all indicators) — gated by rainfall so
  monsoon runoff is labelled "natural cause likely" rather than paged.
- **Prioritises** each candidate 0-100 with a transparent weighted model (or a
  learned model once enough field validations exist), with confidence kept
  separate from severity.
- **Raises alerts** carrying location, date/time, affected polygon and area,
  primary indicator, severity, confidence, indicator readings vs baseline, a
  four-factor explanation, evidence imagery and a one-page PDF brief; dispatches
  them to jurisdiction-matched recipients by webhook or e-mail.
- **Closes the loop**: field teams file samples and lab results against an alert;
  the platform issues a matched / not-matched / inconclusive verdict, widens the
  baseline where it was wrong, and retrains the priority model when it can prove
  an improvement.
- **Shows it all** on a dashboard: map with raster layers and live Earth Engine
  imagery, timeline, indicator panel, series with the seasonal band, a priority
  queue, alert slide-over with validation form, a Methodology tab, and PDF/CSV
  export of any pipeline run.

## Problem statement coverage

| Requirement | How JalNetra meets it | Where |
| --- | --- | --- |
| **Water-body detection** — isolate the body despite cloud, season, shadow, moving boundaries | SCL cloud/shadow/cirrus/snow mask (dilated) → per-scene Otsu on MNDWI inside the registered outline + 60 m → morphology and minimum component size → water extent, water fraction and valid-pixel share stored per pass; scenes with too little valid area are rejected, not guessed | `l04_preprocessing`, `l05_water_detection` |
| **Spectral analysis** — turbidity, sediment, chlorophyll, algal activity, surface anomalies | NDTI, red-reflectance sediment proxy, NDCI, FAI and MNDWI computed over detected water; the spatial detector finds surface patches and plumes | `l06_indicators/registry.py`, `l08_anomaly/spatial.py` |
| **Scientific basis explained** | Each indicator carries its formula, bands, valid range and a scientific-basis paragraph with confounders, served by `GET /api/v1/methodology` and the dashboard's Methodology tab — generated from the code, so it cannot drift | `api/v1/methodology.py`, `features/methodology` |
| **Temporal monitoring** — compare dates, find significant change | Day-of-year seasonal baselines, per-zone series with the p10-p90 band, timeline scrubber, reference-vs-current image pairs in briefs, weekly continuous aggregate | `l07_baseline`, `SeriesChart`, `TimelineScrubber` |
| **Intelligent anomaly detection** — history, spatial patterns, multiple indicators | Temporal robust-z + spatial DBSCAN + multivariate IsolationForest voting; rainfall gate; sigma floors; severity from votes | `l08_anomaly` |
| **Alert generation** — location, date/time, region, indicator, severity/confidence, evidence | Frozen `AlertOut` contract with water body, zone centroid, observed date, affected polygon + km², primary indicator, severity, confidence, priority, indicator readings, context, evidence URLs, timeline and brief | `l11_alerts`, `schemas/alerts.py` |
| **Explainability** — why a region was flagged | Template summary sentence + exactly four signed contributions that sum to the score (deviation, corroboration, extent, rainfall), boundary-checked | `l10_explain` |
| **Product boundary** | Disclaimer on every response, brief and report; `check_boundary` on summaries; validation loop feeds lab truth back | everywhere |

## Architecture

```
 Sentinel-2 L2A  ──►  L3 ingest  ──►  L4/L5 water mask  ──►  L6 indicators  ──►  L7 baselines + rainfall
 (Earth Search /       windowed        SCL gate, Otsu        5 zonal indicators,     DOY median/MAD,
  CDSE / Earth         band reads,     MNDWI, morphology     COG chips, hypertable   Open-Meteo, weekly agg
  Engine)              MinIO cache                                                          │
                                                                                            ▼
 L1 dashboard  ◄──  L2 API + tiles  ◄──  L11/L12 alerts + briefs  ◄──  L9/L10 priority + explain  ◄──  L8 detectors
 React/Leaflet       FastAPI, TiTiler     dedup, PDF, dispatch          weighted / XGBoost, 4 factors    temporal, spatial,
                     proxy, Redis                    ▲                                                     multivariate, rain gate
                                                     │
                                          L13 validation loop (field samples → verdict → baselines / retraining)
```

Every stage is one Celery task, idempotent on (water body, scene), and hands off
to the next automatically (mask → indicators → anomalies → scoring → alerts).
`POST /api/v1/jobs/ingest` starts the chain for one body and window;
`GET /jobs/{id}` derives progress from the stage tables, so restarts never desync it.

| Layer | Stage | Where |
| ----- | ----- | ----- |
| L1 | Dashboard, priority queue, alert sheet, validation form, methodology, live imagery, report export | `frontend/src` |
| L2 | REST API, TiTiler proxy, keyset pagination, Redis cache, rate limiting, API-key auth on writes | `backend/app/api`, `app/services/l02_api` |
| L3 | Scene discovery (Earth Search, CDSE, Google Earth Engine), windowed reads, MinIO band cache | `app/services/l03_ingestion` |
| L4/L5 | SCL cloud gate, MNDWI water mask, usable-pixel share per body | `app/services/l04_preprocessing`, `l05_water_detection` |
| L6 | Zonal indicators into a TimescaleDB hypertable + one COG chip per indicator | `app/services/l06_indicators` |
| L7 | Day-of-year robust baselines, Open-Meteo rainfall, weekly continuous aggregate | `app/services/l07_baseline` |
| L8 | Temporal, spatial and multivariate detectors, mandatory rainfall gate | `app/services/l08_anomaly` |
| L9/L10 | Priority 0-100, four signed contributions + plain-language summary | `app/services/l09_fusion`, `l10_explain` |
| L11/L12 | Alert assembly with 14-day dedup, PDF brief, pipeline-run report, webhook/e-mail dispatch | `app/services/l11_alerts`, `l12_delivery` |
| L13 | Field validation verdicts, precision summary, feedback into baselines, guarded retraining | `app/services/l13_validation` |
| Ops | Prometheus metrics, Grafana dashboard + alert rules, Sentry, Flower, resumable backfill CLI | `app/core/metrics.py`, `app/cli.py`, `infra/` |

**Stack.** Python 3.11 · FastAPI · Celery + Redis · SQLAlchemy 2 / Alembic ·
PostgreSQL 16 + PostGIS + TimescaleDB · MinIO (S3) · TiTiler · rasterio/GDAL,
NumPy, SciPy, scikit-learn, (optional) XGBoost + SHAP · ReportLab + matplotlib ·
earthengine-api · React 18 + TypeScript + Vite · Leaflet · TanStack Query ·
Recharts · Tailwind · Prometheus + Grafana · Docker Compose.

```
backend/    FastAPI app + Celery workers (Python 3.11, uv)
  app/api/        routers (v1: water-bodies, alerts, validations, jobs, imagery, methodology), tiles proxy, middleware
  app/core/       config, logging, metrics, cache, storage, health probes, auth, rate limiting
  app/db/         SQLAlchemy models, sessions, Alembic migrations (0001-0012)
  app/services/   one package per pipeline layer L2-L13 (+ registry, ops)
  app/workers/    Celery app, tasks, beat schedule, signals
  app/schemas/    Pydantic v2 response models (the API contract)
  app/cli.py      resumable backfill, ops gauges
  tests/          unit tests (integration tests behind JALNETRA_INTEGRATION=1)
frontend/   Vite + React 18 + TypeScript dashboard
infra/      compose init SQL, Prometheus + Grafana provisioning
notebooks/  exploration
```

## Feature tour

**Dashboard** (`/`) — water-body list with status and latest observation → Leaflet
map with the body outline, zone polygons, open-alert polygons, per-layer raster
toggles (turbidity, chlorophyll, algae, sediment, extent, water mask) served
through the TiTiler proxy, and a **Live satellite** block (Earth Engine true
colour / false colour / NDTI / NDCI / MNDWI, latest pass or cloud-free composite)
→ timeline scrubber over observations → indicator panel with value, baseline, z
and deviation per zone → Recharts series with the seasonal p10-p90 band.

**Priority queue** (`/alerts`) — every alert ranked by priority with server-side
filters (status, severity, min priority, district); click opens the alert sheet:
priority ring, signed contribution chart (rainfall discount in red), indicator
table, timeline, evidence images, PDF brief, status workflow, and the field
validation form with photo upload and immediate verdict.

**Methodology** (`/methodology`) — the whole method from data to validation,
including every indicator's formula and scientific basis, detector parameters,
priority weights and known limitations, generated from the running code.

**Run pipeline** (header) — pick a window, press run, watch the five stages fill;
when scenes exist, **Report** (PDF) and **CSV** export the run: a summary page
(day table, trends, alerts) and one page per observed day with the satellite
image, water mask, turbidity and chlorophyll rasters and the per-zone data.

`?wb=` and `?alert=` in the URL make any state a shareable demo link.

## Quick start

Docker Desktop with ~20 GB free on its data disk (images plus build cache for the
geo stack run to ~35 GB; a full disk kills the WSL VM mid-build).

```sh
cp .env.example .env
make up          # api :8000, postgres :5432, redis :6379, minio :9000/:9001, titiler :8001
make migrate     # alembic upgrade head
make seed        # 30 Pune-district water bodies + zones
curl localhost:8000/health
```

Without `make` (Windows), run the lines from the `Makefile` directly, e.g.
`docker compose run --rm api alembic upgrade head`.

Then open the dashboard and press **Run pipeline**, or start a job by hand:

```sh
cd frontend && npm install && npm run dev        # http://localhost:5173
curl -X POST localhost:8000/api/v1/jobs/ingest -H 'content-type: application/json' \
  -d '{"water_body_id":"wb_khadakwasla","date_from":"2026-04-20","date_to":"2026-05-10"}'
curl localhost:8000/api/v1/jobs/<job_id>       # status, progress_pct, per-stage counts
```

A three-week window over Khadakwasla finds about five scenes and runs end to end
in roughly four minutes (Earth Search, ~25 s per scene) or under two (Earth
Engine, ~9 s per scene). **Alerts need a seasonal baseline**, so a fresh database
raises none until you [backfill](#backfill-and-baselines).

| Service | Image | Port |
| --- | --- | --- |
| api | `backend/Dockerfile` (uvicorn) | 8000 |
| worker-ingestion / -processing / -scoring / -reporting | same image, one Celery pool per queue | - |
| beat | same image, `celery beat` | - |
| postgres | `timescale/timescaledb-ha:pg16` (PostGIS + TimescaleDB) | 5432 |
| redis | `redis:7-alpine` | 6379 |
| minio | `quay.io/minio/minio` | 9000, 9001 |
| titiler | `ghcr.io/developmentseed/titiler` (pinned to `PORT=8000`) | 8001 |
| flower / prometheus / grafana | `--profile ops` only | 5555 / 9090 / 3000 |

## Google Earth Engine setup

Optional, but it makes ingestion 3x faster and unlocks the live imagery layer and
true-colour report images. Earth Engine has **no API keys**; it authenticates a
Google identity that has been registered for Earth Engine on a Cloud project.

1. Create a Google Cloud project, enable the *Google Earth Engine API*, and
   register the project at https://console.cloud.google.com/earth-engine/configuration
   (non-commercial / research use is free). Note the project **ID** (e.g.
   `steel-league-509405-q6`), not the display name.
2. Authenticate, one of:
   - **Dev box:** `cd backend && uv run earthengine authenticate` (Google sign-in;
     credentials land in `~/.config/earthengine`, which docker-compose mounts
     read-only into the backend containers — `EARTHENGINE_CREDENTIALS_DIR`).
   - **Server:** a service account with the *Earth Engine Resource Viewer* role;
     put its JSON key at `backend/data/secrets/gee-service-account.json`
     (git-ignored) and set `GEE_SERVICE_ACCOUNT_KEY=data/secrets/gee-service-account.json`
     (or paste it into `GEE_SERVICE_ACCOUNT_KEY_JSON`).
3. In `.env`: `GEE_ENABLED=true`, `GEE_PROJECT=<project id>`; optionally
   `STAC_SOURCE=gee` to make it the primary ingestion source.
4. `docker compose up -d --force-recreate api worker-ingestion`, then
   `curl localhost:8000/api/v1/imagery/status` → `ok: true`, and `/health` gains a
   `gee` probe.

## Backfill and baselines

Seasonal baselines need multi-year history: a day-of-year window is `usable`
with ≥ 5 samples and ≥ 730 days of history for Tier 1 bodies (365 otherwise).
Until then every zone reads "baseline building" and anomalies are suppressed
with that reason.

```sh
make backfill wb=wb_khadakwasla from=2024-04-01 to=2026-09-22   # resumable; re-run the same line after an interruption
make backfill-status
# faster via Earth Engine:
docker compose run --rm -e STAC_SOURCE=gee api python -m app.cli backfill --water-body wb_khadakwasla --from 2024-04-01 --to 2026-09-22 --resume
```

The CLI runs the whole chain in-process in 31-day chunks (ingest → mask →
indicators → detectors → scores → alerts), syncs rainfall, checkpoints on a
`jobs` row, and builds the baselines at the end. Measured: 2.5 years of
Khadakwasla, 167 scenes, ~35 minutes via Earth Engine.

## Configuration

Every setting lives in `backend/app/core/config.py` and is read from the
environment / `.env`; `.env.example` lists them all with defaults. The ones you
are most likely to touch:

| Group | Keys |
| --- | --- |
| App | `APP_ENV`, `LOG_LEVEL`, `CORS_ORIGINS`, `PUBLIC_BASE_URL` |
| Infrastructure | `DATABASE_URL`, `REDIS_URL`, `CELERY_*`, `MINIO_*`, `TITILER_URL` |
| Ingestion | `STAC_SOURCE` (`earth-search` \| `cdse` \| `gee`), `STAC_FALLBACK`, `STAC_MAX_CLOUD_PCT`, `CDSE_CLIENT_ID/SECRET`, `INGEST_LOOKBACK_DAYS*` |
| Earth Engine | `GEE_ENABLED`, `GEE_PROJECT`, `GEE_SERVICE_ACCOUNT_KEY[_JSON]`, `GEE_COLLECTION`, `GEE_HIGH_VOLUME`, `GEE_MAP_TTL_S`, `GEE_LIVE_MAX_DAYS`, `GEE_LIVE_MAX_BBOX_DEG` |
| Water mask / indicators | `MASK_MIN_VALID_PCT`, `INDICATOR_MIN_VALID_PCT`, `INDICATOR_MIN_PIXELS` |
| Baselines | `BASELINE_WINDOW_DAYS`, `BASELINE_MIN_SAMPLES`, `BASELINE_MIN_HISTORY_DAYS[_TIER1]`, `BASELINE_HISTORY_YEARS`, `RAINFALL_*` |
| Detection | `ANOMALY_Z_THRESHOLD`, `ANOMALY_Z_HIGH`, `ANOMALY_SIGMA_FLOOR`, `SPATIAL_*`, `MULTIVARIATE_*`, `RAINFALL_GATE_*` |
| Priority | `PRIORITY_Z_SATURATION`, `PRIORITY_TRAIN_MIN_VALIDATIONS`, `PRIORITY_MODEL_PREFIX` |
| Alerts / delivery | `ALERT_DEDUPE_DAYS`, `ALERT_MIN_PRIORITY`, `DISPATCH_ENABLED` (default **false**), `SMTP_*`, `BRIEF_PREFIX` |
| Reports | `REPORT_SATELLITE_SOURCE` (`auto` \| `gee` \| `cache`), `REPORT_MAX_DAY_PAGES`, `REPORT_IMAGE_PX` |
| API | `API_KEYS` (`{"<key>": "<actor>"}`; empty = open in dev/test, refused in prod), `RATE_LIMIT_*`, `API_CACHE_*` |
| Validation | `VALIDATION_THRESHOLDS`, `VALIDATION_MAX_LAG_DAYS`, `RETRAIN_*` |
| Ops | `SENTRY_DSN`, `WORKER_METRICS_PORT`, `WORKERS_*`, `TASK_*_TIME_LIMIT_S`, `TIER1_STALE_DAYS` |

## API reference

Interactive docs at http://localhost:8000/docs. All product endpoints are under
`/api/v1`; write endpoints require `X-API-Key` when `API_KEYS` is set.
`tests/test_api.py` asserts this inventory.

| Endpoint | Purpose |
| --- | --- |
| `GET /health`, `GET /metrics` | Dependency probes (postgres, redis, minio, stac, gee when enabled); Prometheus scrape |
| `GET /water-bodies`, `/water-bodies/{id}` | Registry with status, latest observation, zones (GeoJSON), bbox |
| `GET /water-bodies/{id}/observations` | Passes with coverage and water extent (keyset cursor) |
| `GET /water-bodies/{id}/indicators?date=&zone=` | Per-zone readings vs baseline for a date |
| `GET /water-bodies/{id}/series?indicator=&zone=&from=&to=` | Series with the seasonal p10-p90 band (Redis-cached) |
| `GET /alerts`, `/alerts.geojson`, `/alerts/{id}` | Priority-ordered alerts, map layer, full alert with explanation and evidence |
| `GET /alerts/{id}/brief.pdf`, `/alerts/{id}/geometry.geojson` | One-page investigation brief; affected polygon |
| `PATCH /alerts/{id}/status` | open → investigating → validated \| dismissed, audited |
| `POST /validations`, `POST /validations/{id}/photo`, `GET /validations`, `/validations/summary` | Field results with verdict on submit; precision to date |
| `POST /jobs/ingest`, `GET /jobs`, `/jobs/{id}` | Start and watch a pipeline run (chunked backfill for windows > 62 days) |
| `GET /jobs/{id}/report.pdf`, `/jobs/{id}/report.csv` | Analysis report of a run, day by day, with satellite images and zone data |
| `GET /imagery/status`, `GET /imagery/live?bbox=&vis=&date=&days=&composite=` | Earth Engine session status; styled live map id (XYZ template served by Google) |
| `GET /methodology` | The method, indicators with scientific basis, parameters, limitations |
| `GET /tiles/{layer}/{water_body_id}/{date}/{z}/{x}/{y}.png`, `/tiles/chip/{key}/...`, `/tiles/styles` | Styled raster tiles for indicator, water-mask and anomaly chips |

## Layer-by-layer details

### Water body registry

```sh
make seed                                   # 30 Pune-district bodies from OSM outlines, 4-8 zones each
make load f=my_bodies.geojson d=Nashik      # any GeoJSON / shapefile; props: name, tier, kind, drinking_water, urban
make mgrs-grid                              # optional: exact Sentinel-2 tile lookup from ESA's KML grid
```

- Tiering: Tier 1 = drinking-water sources, urban river stretches, or ≥ 10 km²;
  Tier 2 ≥ 1 km²; Tier 3 the rest. An explicit `tier` property wins. Tier drives
  the beat cadence (6 h / daily / weekly) and baseline history requirements.
- Zones: k-means over pixel centroids → Voronoi cells clipped to the body, so
  zones tile it exactly; regenerated only when the geometry changes.
- MGRS tiles: computed arithmetically by default; `make mgrs-grid` switches to
  the exact ESA grid.

### Satellite ingestion (L3)

- **Sources**: Earth Search (AWS, no auth, default), CDSE (OAuth2 client
  credentials), Google Earth Engine (`COPERNICUS/S2_SR_HARMONIZED`, opt-in).
  `ChainedSource` tries the primary and falls back; `scenes.source` records who
  served each scene, and metrics count failures and fallbacks.
- **Windowed reads only.** Bands B03 B04 B05 B08 B11 SCL for the body's bounding
  box (+100 m), snapped to the 20 m grid so every band lands on one shared 10 m
  grid (20 m bands bilinear, SCL nearest). COG sources go through `/vsicurl/`
  range requests; Earth Engine goes through `computePixels` on the identical grid
  in ≤ 1024 px blocks — verified pixel-identical to the COG read.
- **Reflectance offset per scene.** Raw ESA L2A stores `DN = (rho + 0.1) * 10000`
  since baseline 04.00, Earth Search serves COGs with the offset removed, and the
  harmonized Earth Engine collection removes it too; `scenes.boa_add_offset`
  records the right value and L6 applies it, so indicators are comparable across
  sources.
- Arrays are cached in MinIO at `cache/{water_body_id}/{scene_id}/bands.npz`;
  `scene_ingestions` makes the task idempotent. Transient failures retry with
  exponential backoff.

### Water-body detection (L4 + L5)

- SCL classes 3, 8, 9, 10, 11 (cloud shadow, cloud medium/high, cirrus, snow)
  masked and dilated; 0, 1 no-data. A scene below `MASK_MIN_VALID_PCT` valid
  area over the body is rejected.
- MNDWI = (B3 − B11)/(B3 + B11); Otsu's threshold fitted per scene on valid
  pixels inside the search area, trusted only within a plausible band, otherwise
  a fixed fallback — the choice is stored (`threshold_method`) per scene.
- 3×3 opening/closing, minimum component size, intersection with the registered
  outline buffered 60 m. Output: a COG water-mask chip (`chips/{body}/{date}/body/watermask.tif`)
  and `water_masks` row with valid %, cloud %, water extent km², water fraction.

### Spectral indicators (L6)

| Key | Indicator | Formula | Basis (short) |
| --- | --- | --- | --- |
| `ndti_turbidity` | Turbidity | (B4 − B3)/(B4 + B3) | sediment scatters red more than green |
| `ndci_chlorophyll` | Chlorophyll-a | (B5 − B4)/(B5 + B4) | phytoplankton absorb red, reflect at the red edge |
| `fai_algal` | Floating algae | B8 − [B4 + (B11 − B4)·(842−665)/(1610−665)] | surface scums reflect NIR like vegetation |
| `sediment_proxy` | Suspended sediment | B4 reflectance over water | clear water absorbs red, sediment scatters it back |
| `mndwi_extent` | Water extent | (B3 − B11)/(B3 + B11), whole zone | tracks draw-down and refill |

Full scientific basis and confounders: `GET /api/v1/methodology` or
`l06_indicators/registry.py`. Each pass writes one `indicator_observations` row
per zone and indicator (mean, p90, std, pixels, valid %, water fraction) into a
TimescaleDB hypertable, and one COG chip per indicator.

### Seasonal baseline + rainfall (L7)

- `baselines`: one row per (zone, indicator, day-of-year) summarising every
  observation in a 30-day circular DOY window with **median** and **1.4826·MAD**,
  p10/p90, `n_samples`, `n_years`. A single historical spike moves the centre by
  < 5 %. Status (`usable` / `building`) is recomputed on read.
- `rainfall`: daily `mm_24h` / `mm_72h` at each body from Open-Meteo (ERA5 archive
  for history, forecast API for the ~5-day archive lag).
- `indicator_weekly`: TimescaleDB continuous aggregate (hourly policy over the
  last 90 days; `refresh_weekly` covers the archive after a backfill).
- Beat: rainfall daily 02:00 IST, baseline rebuild monthly.

### Anomaly detection (L8)

Three detectors vote per zone per scene; one `anomaly_candidates` row is written
per zone whether or not anything fired.

- **Temporal** — `z = (x − median) / max(1.4826·MAD, floor)` against the DOY
  baseline; flagged at |z| ≥ 3, only when the baseline is `usable`.
- **Spatial** — per-pixel robust z of each indicator chip against the body's own
  water pixels in the same scene, DBSCAN (eps 3 px, min 10) over hot pixels;
  clusters ≥ 0.05 km² become WGS84 polygons attributed to the zone holding most
  of their pixels. > 30 % hot water is a body-wide shift, not a plume.
- **Multivariate** — IsolationForest (contamination 0.05) on
  `[ndti, ndci, fai, sediment, water_extent_delta, rainfall_72h]` fitted on the
  zone's own history (≥ 20 scenes, current scene excluded).
- **Severity** — 3 votes → high; 2 → high if max|z| ≥ 5 else medium; 1 → medium
  if max|z| ≥ 5 else low.
- **Rainfall gate (mandatory)** — if 72 h rain exceeds the seasonal p90 and only
  turbidity/sediment deviate, `natural_cause_likely = true` and severity is
  capped at medium; chlorophyll and FAI are never gated. The decision is stored
  for the explanation panel.
- `alertable` requires a severity **and** a usable baseline; otherwise
  `suppressed_reason` says why.

### Fusion, priority and explainability (L9 + L10)

- **Features**: temporal z per indicator (positive direction only), √(cluster
  area / zone area), indicators deviating together, growth vs previous pass,
  IsolationForest score, rainfall seasonal percentile, cloud-free share, zone
  share of the body.
- **Priority 0-100**: `weighted-v1` (weights documented in `l09_fusion/models.py`;
  rainfall is the one negative weight, cloud has weight 0 on purpose). An XGBoost
  regressor trained on validated outcomes can be activated; failures fall back to
  the weighted model loudly.
- **Severity** = score bands (< 40 low, 40-69 medium, ≥ 70 high) then the
  rainfall cap. **Confidence** = √(cloud-free share) × (baseline depth + detector
  agreement)/2, kept separate from severity.
- **Explanation**: four signed contributions summing to `score − base` — primary
  deviation, corroborating indicators, spatial extent, rainfall — same schema for
  the weighted and SHAP paths; a fixed-template summary sentence that
  `check_boundary` rejects if it names pollution, contamination or discharge.

### Alerts, briefs and reports (L11 + L12)

- **Assembler**: an alertable score becomes an `alerts` row
  (`alr_{YYYY}_{MMDD}_{body}_z{seq}`). An open/investigating alert for the same
  zone + indicator within 14 days absorbs new observations (current fields
  refresh, `peak_*` kept, `timeline` appended, non-alertable follow-ups appended
  too so the episode is seen subsiding).
- **Contract** (`app/schemas/alerts.py`): frozen, `disclaimer` verbatim on every
  response; additions only.
- **Brief**: one A4 page — header, explanation, indicator table vs baseline,
  evidence timeline with the seasonal band, reference-vs-current image pair, the
  four contributions as a signed bar chart, field-sampling checklist, disclaimer.
  Stored at `briefs/{alert_id}.pdf`.
- **Pipeline-run report**: `GET /jobs/{id}/report.pdf|csv` — summary page (day
  table with status / cloud / coverage / water km² / body-wide indicator means /
  flagged zones / max priority / rain, trend charts, alerts in the window) then
  one page per observed day with the satellite image (Earth Engine true colour,
  or NIR false colour from cached bands), water mask, turbidity and chlorophyll
  rasters and the per-zone table; CSV is one row per (day, zone). Finished jobs
  are rendered once and stored at `reports/{job_id}.*`.
- **Dispatch**: `recipients` (webhook HMAC-signed or HTML e-mail) with
  `min_severity` and a jurisdiction (districts or polygon), on creation and
  escalation only. `DISPATCH_ENABLED=false` by default.

### Validation loop (L13)

- **Verdict on submit**: the lab value matching the alert's primary indicator
  (turbidity/TSS for NDTI and sediment, chlorophyll-a for NDCI and FAI) vs a
  screening threshold → `matched` / `not_matched`; a sample > 5 days after the
  observation or without the key measurement is `inconclusive`.
- Alert status follows the verdict (validated / dismissed / investigating),
  audited with who and why; each validation snapshots the alert's severity,
  indicator and priority so later rescoring cannot rewrite the record.
- `not_matched` writes the satellite values into `baseline_samples`, unioned into
  the seasonal history, so a field-confirmed normal day widens the band.
- `GET /validations/summary` = precision overall, by indicator, by severity.
- **Retraining** (weekly beat + manual task): below 50 conclusive validations it
  exits with a logged reason; otherwise fits XGBoost, measures precision on a
  held-out split against the active model, and promotes only if strictly better.

## Frontend

```sh
cd frontend
npm install
npm run dev            # http://localhost:5173, proxies /api, /tiles, /health to :8000
npm run gen:api        # regenerate src/api/schema.d.ts from ./openapi.json
npm run build          # tsc -b && vite build
```

Export the schema with
`cd backend && uv run python -c "import json; from app.main import app; json.dump(app.openapi(), open('../frontend/openapi.json','w'), indent=1)"`.

- **Typed client only.** `src/api/types.ts` aliases the generated OpenAPI types;
  `src/api/client.ts` is the only module touching the transport. No `fetch` in
  components, no mock data.
- **Structure**: `features/dashboard` (list, map, live imagery, timeline,
  indicator panel, series), `features/alerts` (queue, sheet, validation form),
  `features/jobs` (pipeline runner + report export), `features/methodology`,
  `store/ui.ts` (zustand UI state), TanStack Query for all server data.
- The disclaimer comes from the API on every alert view, list, feed and
  indicator panel; never hardcoded, never hidden.

## Operations and observability

```sh
make ops                                   # stack + Flower :5555, Prometheus :9090, Grafana :3000 (admin / jalnetra)
docker compose run --rm api python -m app.cli ops-gauges
```

- **Queues by cost** — `ingestion`, `processing`, `scoring`, `reporting`, one
  worker pool each (`WORKERS_*`), so a slow PDF never blocks a scene read.
- **Beat** — Tier 1 poll every 6 h, Tier 2 daily, Tier 3 weekly; rainfall daily;
  baseline rebuild monthly; ops gauges every 15 min; retraining attempt weekly.
- **Prometheus** — scenes ingested / rejected, zones processed, alerts raised /
  gated, task duration by stage, STAC failures and fallbacks, dispatch outcomes;
  gauges for days since last usable scene per Tier 1 body, open alerts,
  baseline-building zones, validation precision. API at `/metrics`, workers on
  `:9100`.
- **Grafana** — provisioned dashboard `infra/grafana/dashboards/jalnetra-pipeline.json`;
  alert rules in `infra/prometheus/alerts.yml` (`Tier1WaterBodyStale`, STAC
  failing, slow stages, worker metrics down, dispatch failures).
- **Sentry** — `SENTRY_DSN`; every task event tagged with body / scene / alert.
- **Task limits** — soft/hard time limits so a hung range read frees its worker;
  with `acks_late` the task is redelivered.

## Development

```sh
cd backend
uv sync                                        # Python 3.11 venv
uv run ruff check . && uv run ruff format --check . && uv run mypy .
uv run pytest                                  # unit suite (~160 tests)
docker compose run --rm -e JALNETRA_INTEGRATION=1 api pytest   # + live-stack tests
uv run uvicorn app.main:app --reload
cd ../frontend && npx tsc -b && npx vite build
```

Conventions: strict mypy, ruff (E F I B UP N W SIM RUF), Pydantic v2 schemas as
the contract, every stage idempotent on (water body, scene), never "pollution"
in generated text. No hosted CI at the moment — run the checks above before
committing.

## Status, verified results and known issues

**Built and live-verified (2026-09-22):** all thirteen layers; migrations
0001-0012; 30 seeded bodies; Earth Engine session, live layer, `computePixels`
ingestion (pixel-identical to Earth Search, 3x faster); pipeline-run PDF/CSV
reports; methodology endpoint and tab.

**Real alerts, end to end.** A 2.5-year Khadakwasla backfill (Apr 2024 - Sep
2026, 167 scenes via Earth Engine, ~35 min) built 5,275 usable day-of-year
windows. Re-running detection, scoring and assembly over that history produced
75 alerts (6 high, 15 medium) with briefs and a populated priority queue — e.g.
`alr_2024_1224_khadakwasla_z3`: suspended sediment 17σ above its December
baseline with turbidity, chlorophyll and FAI rising together, a 0.08 km² patch,
3/3 detectors voting, 0 mm rain, priority 93. These are hindcast alerts (each
day was scored against a baseline that includes its own year).

**Known issues**

- A body straddling two MGRS tiles can get two scenes on the same date; chips
  are keyed by body + date, so the second chip overwrites the first and L6 fails
  with a shape mismatch for that date (seen on Nira Deoghar, 43QCA/43QCV). Needs
  per-tile mosaicking.
- Only Khadakwasla has baselines; the other 29 bodies show "baseline building"
  until `make backfill` runs for them (~30 min each via Earth Engine).
- The `ops` profile (Flower, Prometheus, Grafana) and dispatch to real
  recipients have not been exercised live.
- Optical limits by design: nothing is observed under cloud or below the surface;
  dissolved contaminants with no optical signature are invisible; indicators are
  proxies, not concentrations.

## Data sources and attribution

- Sentinel-2 L2A: *Contains modified Copernicus Sentinel data*, via AWS Earth
  Search (Element 84), Copernicus Data Space Ecosystem, and Google Earth Engine
  (*processed in Google Earth Engine*).
- Rainfall: Open-Meteo (ERA5 archive and forecast APIs).
- Water body outlines: © OpenStreetMap contributors (ODbL), fetched via Overpass.
- MGRS grid: ESA Sentinel-2 tiling grid KML.
