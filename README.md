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
frontend/   Vite + React 18 + TypeScript dashboard (S10)
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

## Seasonal baseline + rainfall (S5, L7)

```sh
# bulk history for a body: 3 years of scenes in 31-day ingest chunks (each chains mask -> indicators) + Open-Meteo archive
docker compose run --rm api python -c "from app.workers.tasks import backfill_history as t; print(t.delay('wb_khadakwasla').get())"
# once the processing queue drains, build the baselines
docker compose run --rm api python -c "from app.workers.tasks import build_baselines as t; print(t.delay('wb_khadakwasla').get())"
```

- `baselines`: one row per (zone, indicator, day-of-year). Each row summarises
  every observation within a 30-day window centred on that DOY (circular, so
  late Dec and early Jan share a window) with **median** (`mean` column) and
  **1.4826·MAD** (`std` column), plus p10/p90, `n_samples`, `n_years`.
  A single historical spike moves the centre by <5 % (`tests/test_baseline.py`).
- A window is `usable` only with ≥ `BASELINE_MIN_SAMPLES` (5) observations and
  enough history span (Tier 1: 730 days, others 365); otherwise `building` and
  L8 must suppress alerts. Status is recomputed on read, so raising a threshold
  needs no rebuild.
- Read side: `get_baseline(session, zone_id, indicator, date)`,
  `get_baseline_year`, `series_with_band` (points + DOY band for the chart),
  `zone_baseline_status`.
- `rainfall`: daily `mm_24h` / `mm_72h` at each body's point-on-surface from
  Open-Meteo — the ERA5 **archive** for history, the **forecast** API's
  `past_days` for the ~5-day archive lag (archive rows overwrite forecast rows,
  never the reverse). `get_rainfall_context(session, water_body_id, date)` adds
  `mm_7d` and `available=False` when the day is missing.
- `indicator_weekly` is a TimescaleDB continuous aggregate (weekly zone-indicator
  means, hourly refresh policy over the last 90 days; `refresh_weekly` covers the
  archive after a backfill). Read with `weekly_series`.
- Beat: `sync_rainfall_all` daily 02:30 UTC, `rebuild_all_baselines` nightly 03:00 UTC.

## Anomaly detection (S6, L8)

```sh
# one scene (normally chained automatically after compute_indicators)
docker compose run --rm api python -c "from app.workers.tasks import detect_anomalies as t; print(t.delay('wb_khadakwasla','S2C_43QCA_20260503').get())"
# backfill a window, oldest first so each scene sees only the history it would have had
docker compose run --rm api python -c "from app.workers.tasks import process_anomalies as t; print(t.delay('wb_khadakwasla','2026-01-01','2026-09-21').get())"
```

Three detectors vote per zone per scene; none decides alone. One
`anomaly_candidates` row is written per zone whether or not anything fired.

- **Temporal** — robust z of the zone mean against its DOY baseline,
  `z = (x − median) / max(1.4826·MAD, floor)`; flagged at |z| > 3, only when the
  baseline is `usable`. Per-indicator sigma floors (`ANOMALY_SIGMA_FLOOR`) stop a
  near-constant history from turning sensor noise into a huge z.
- **Spatial** — per-pixel robust z of each L6 indicator chip against the body's
  own water pixels *in the same scene*, DBSCAN (eps 3 px, min_samples 10) over
  hot pixels, clusters ≥ 0.05 km² become real WGS84 polygons (`spatial_geom`,
  `affected_area_km2`) attributed to the zone holding most of their pixels.
  If > 30 % of the water is hot it is a body-wide shift, not a plume, and the
  temporal detector owns the call.
- **Multivariate** — IsolationForest (contamination 0.05) on
  `[ndti, ndci, fai, sediment, water_extent_delta, rainfall_72h]` fitted on the
  zone's own history (≥ 20 scenes; the current scene excluded).
- **Severity** (provisional; L9 fuses confidence/priority): 3 votes → high;
  2 votes → high if max|z| ≥ 5 else medium; 1 vote → medium if max|z| ≥ 5 else low.
- **Rainfall gate (mandatory)** — if `mm_72h` exceeds the body's seasonal p90
  (DOY window over the `rainfall` table) *and* only turbidity/sediment deviate,
  `natural_cause_likely = true` and severity is capped at `medium`. Chlorophyll
  and FAI deviations are never gated. The full decision is stored in
  `rainfall_gate` (with `capped_from`) so the explanation panel can show it.
- `alertable` is true only with a severity **and** a usable baseline; otherwise
  `suppressed_reason` says why ("baseline building…"). Nothing in this layer
  says "pollution": a candidate is an observable deviation.

## Fusion, priority and explainability (S7, L9 + L10)

```sh
# scoring is chained automatically after detect_anomalies; rescore a window under the active model
docker compose run --rm api python -c "from app.workers.tasks import process_scores as t; print(t.delay('wb_khadakwasla','2026-01-01','2026-09-21', True).get())"
# once >= 50 validations exist (S11) and `uv sync --extra ml` is installed:
docker compose run --rm api python -c "from app.workers.tasks import train_priority_model as t; print(t.delay(activate=True).get())"
```

One `candidate_scores` row per anomaly candidate, stamped with `model_version`.

- **Feature vector** (`l09_fusion/features.py`): temporal z per indicator
  (positive direction only — clearer-than-usual water earns nothing), cluster
  area / zone area (√), indicators deviating together, growth vs the previous
  pass, IsolationForest score, rainfall seasonal percentile, cloud-free share,
  zone share of the body. Raw and normalised forms are both stored.
- **Priority 0–100**: `weighted-v1` is the documented, always-available model
  (weights in `l09_fusion/models.py`; rainfall is the one negative weight and
  only bites above the seasonal median; cloud has weight 0 on purpose). An
  XGBoost regressor trained on validated outcomes can be registered in
  `priority_models` and activated; loading failures fall back to the weighted
  model loudly.
- **Severity** = score bands (low < 40, medium 40–69, high ≥ 70), then the S6
  rainfall cap. **Confidence** = √(cloud-free share) × (baseline depth +
  detector agreement)/2 — kept separate: a big deviation seen through 45 %
  cloud is high severity at ~0.5 confidence.
- **Explanation** (`l10_explain/explain.py`): exactly four signed
  contributions summing to `score − base` — primary deviation, corroborating
  indicators, spatial extent, rainfall (always rendered, negative when it
  discounts) — same schema for the weighted path (weight × feature) and the
  SHAP path. The summary follows the plan's template ("Flagged because the
  turbidity indicator is 2.4x its seasonal baseline across 2.47 km2 of Eastern
  zone, with a correlated rise in suspended sediment…") and `check_boundary`
  rejects any summary containing pollution/contamination/discharge language.

## Alerts and reports (S8, L11 + L12)

```sh
# alerts are assembled automatically after scoring; backfill a window (no briefs/dispatch unless briefs=True)
docker compose run --rm api python -c "from app.workers.tasks import process_alerts as t; print(t.delay('wb_khadakwasla','2026-01-01','2026-09-21').get())"
curl localhost:8000/api/v1/alerts?status=active&severity=medium          # priority descending
curl localhost:8000/api/v1/alerts.geojson                                 # map layer, priority_score in properties
curl -o brief.pdf localhost:8000/api/v1/alerts/alr_2026_0917_khadakwasla_z3/brief.pdf
curl -X PATCH localhost:8000/api/v1/alerts/<id>/status -H 'content-type: application/json' -d '{"status":"investigating","by":"RO Pune","note":"team dispatched"}'
```

- **Assembler** (`l11_alerts/assembler.py`): an alertable `candidate_scores`
  row becomes an `alerts` row (`alr_{YYYY}_{MMDD}_{body}_z{seq}`). **Dedup**: an
  open/investigating alert for the same zone + primary indicator within 14 days
  absorbs the new observation — current fields refresh, `peak_*` is kept, and
  the observation is appended to `timeline`. Non-alertable observations of the
  same zone are appended too, so the timeline shows the episode subsiding.
- **Contract** (`app/schemas/alerts.py`): the plan's alert JSON, field for
  field, with `disclaimer` verbatim on every response; additions only, no renames.
- **Endpoints**: `GET /api/v1/alerts`, `/alerts.geojson`, `/alerts/{id}`,
  `/alerts/{id}/geometry.geojson`, `/alerts/{id}/brief.pdf` (generated on first
  request if the worker hasn't yet), `PATCH /alerts/{id}/status`, and a
  `webhook-preview` dry run.
- **Brief** (`l12_delivery/brief.py`): one A4 page via ReportLab + matplotlib —
  header, explanation, indicator table (current vs baseline), evidence timeline
  with the seasonal band, reference-vs-current image pair from the L6 chips
  (reference = same season, previous year), the four contributions as a signed
  bar chart, field-sampling checklist and the disclaimer. Stored at
  `briefs/{alert_id}.pdf`; ~60–90 KB.
- **Dispatch** (`l12_delivery/dispatch.py`): `recipients` (webhook or e-mail)
  with `min_severity` and a jurisdiction — district names or a polygon matched
  point-in-polygon on the zone — get the alert JSON (HMAC-signed) or an HTML
  e-mail on creation and on escalation only. `DISPATCH_ENABLED=false` by
  default: a dev box logs `skipped` rows and never pages a regional office.
- Worker queue `reporting` carries briefs and dispatches; compose already lists it.

## Backend API and tile server (S9, L2)

```sh
uv run uvicorn app.main:app --reload         # then open http://localhost:8000/docs
curl localhost:8000/api/v1/water-bodies?district=Pune
curl localhost:8000/api/v1/water-bodies/wb_khadakwasla/series?indicator=ndti_turbidity&zone=wb_khadakwasla_z3&from=2026-01-01
curl -X POST localhost:8000/api/v1/jobs/ingest -H 'content-type: application/json' -d '{"water_body_id":"wb_khadakwasla","date_from":"2026-09-01","date_to":"2026-09-21"}'
curl localhost:8000/api/v1/jobs/<job_id>       # state, progress_pct, current_stage, per-stage counts
curl -o t.png localhost:8000/tiles/turbidity/wb_khadakwasla/2026-09-17/13/5776/3651.png
```

Every endpoint of the frozen contract is mounted under `/api/v1` (plus
`/tiles/...` and `/health`); `tests/test_api.py` asserts the inventory and that
the demo-facing models carry `/docs` examples.

- **Async throughout.** Handlers use the async engine; the read models in
  `app/services/l02_api/` are sync SQLAlchemy executed through
  `AsyncSession.run_sync`, so L7's baseline functions are reused verbatim and
  the event loop never blocks.
- **Cursor pagination** (`app/api/pagination.py`): opaque keyset cursors on
  water bodies, observations, alerts, validations and jobs — `next_cursor`
  in every list response.
- **Redis cache** (`app/core/cache.py`): series and indicators cached 5 min
  under a key that includes the body's latest scene id; fail-open on outage.
  Responses carry `cached: true|false`.
- **Jobs**: `POST /jobs/ingest` enqueues `ingest_water_body` (or the chunked
  `backfill_history` for windows > 62 days) and returns a job id; `GET /jobs/{id}`
  *derives* progress from the stage tables — scenes ingested / masked /
  indicators / anomalies / scored — so retries and restarts never desync it.
- **Tiles**: `/tiles/chip/{chip_key}/{z}/{x}/{y}.png` (what alert evidence
  links carry) and `/tiles/{layer}/{water_body_id}/{date}/{z}/{x}/{y}.png`
  proxy TiTiler with a fixed style per layer — turbidity amber (`ylorbr`),
  chlorophyll green, algae yellow-green, sediment orange, extent blue, water
  mask a single blue, anomaly red. `/tiles/styles` feeds the legend.
- **Rate limiting** (slowapi): `RATE_LIMIT_DEFAULT` per client IP, a higher
  `RATE_LIMIT_TILES` on tiles. **/health** now also probes the STAC source.
- `validations` table + `POST/GET /validations` store field results now;
  verdicts, photo upload and the precision summary land in S11.

## Frontend (S10, L1)

```sh
cd frontend
npm install
npm run dev            # http://localhost:5173, proxies /api, /tiles, /health to :8000
npm run gen:api        # regenerate src/api/schema.d.ts from ./openapi.json (export it from the backend first)
npm run build          # tsc -b && vite build
```

Export the schema with
`cd backend && uv run python -c "import json; from app.main import app; json.dump(app.openapi(), open('../frontend/openapi.json','w'), indent=1)"`.

- **Typed client only.** `src/api/types.ts` aliases the generated OpenAPI
  types; `src/api/client.ts` is the only module that touches the transport
  (`http.ts`). No `fetch` in components, no mock data — the app runs against
  the real backend.
- **Screens**: dashboard (water body list → Leaflet map with body boundary,
  zone polygons, open-alert polygons and per-layer raster toggles pointed at
  `/tiles/{layer}/{water_body_id}/{date}/{z}/{x}/{y}.png`; timeline scrubber
  over observations; indicator panel with baseline/z/deviation; Recharts series
  with the seasonal p10–p90 band), priority queue (server-side filters),
  alert slide-over (priority ring, signed contribution chart with the rainfall
  discount in red, indicators, timeline, evidence + PDF brief, status workflow,
  field validation form).
- **Disclaimer** comes from the API response on every alert view, list, feed
  and indicator panel (`components/Disclaimer.tsx`); never hardcoded, never hidden.
- **Pipeline readout**: "Run pipeline" posts `/jobs/ingest`, polls
  `/jobs/{id}` every 2 s, shows the stage in flight and per-stage bars, and
  invalidates every panel when the job finishes.
- Loading skeletons, empty and error states on every panel; `?wb=` and
  `?alert=` in the URL make any demo state a shareable link.

## Validation loop (S11, L13)

```sh
curl -X POST localhost:8000/api/v1/validations -H 'content-type: application/json'   -d '{"alert_id":"alr_2026_0917_khadakwasla_z3","sampled_on":"2026-09-19","lab_results":{"turbidity_ntu":48},"submitted_by":"RO Pune"}'
curl -F file=@site.jpg localhost:8000/api/v1/validations/12/photo
curl localhost:8000/api/v1/validations/summary            # precision to date, by indicator and severity
docker compose run --rm api python -c "from app.workers.tasks import train_priority_model as t; print(t.delay().get())"
```

- **Verdict on submit** (`l13_validation/verdict.py`): the lab measurement
  that corresponds to the alert's primary indicator (turbidity/TSS for NDTI and
  sediment, chlorophyll-a for NDCI and FAI) is compared with a configurable
  screening threshold (`VALIDATION_THRESHOLDS`: 10 NTU, 30 mg/L, 20 µg/L) →
  `matched` / `not_matched`; a sample more than 5 days after the observation or
  without the key measurement is `inconclusive`. The reason is stored verbatim.
- **Alert status moves**: matched → `validated`, not_matched → `dismissed`,
  inconclusive → `investigating`, audited with who and the verdict reason.
- **Feedback into baselines**: a `not_matched` verdict writes the satellite
  values the alert was raised on into `baseline_samples`; L7's `load_history`
  unions them into the seasonal history, so a field-confirmed normal day widens
  the band where it was wrong.
- **Precision summary** `GET /validations/summary` = matched / (matched +
  not_matched), overall, by indicator, by severity; each validation snapshots
  the alert's severity/indicator/priority at submission so later rescoring
  cannot rewrite the record.
- **Retraining** (`l13_validation/training.py`, weekly beat + manual task):
  exits cleanly with a logged reason below 50 conclusive validations; otherwise
  fits XGBoost on a training split, measures precision at the alert threshold
  on a held-out split for both the candidate and the active model, and promotes
  only if strictly better. A worse model is recorded in `priority_models` but
  never activated. Requires `uv sync --extra ml`.
- Photos go to MinIO `validations/{id}/…`; the frontend form uploads one after
  submit and shows the verdict and its reason immediately.

## Scale, ops and observability (S12)

```sh
make ops                                   # stack + Flower :5555, Prometheus :9090, Grafana :3000 (admin / jalnetra)
make backfill wb=wb_khadakwasla from=2023-01-01 to=2026-09-01   # resumable; re-run the same line after an interruption
make backfill-status
docker compose run --rm api python -m app.cli ops-gauges
```

- **Queues by cost** — `ingestion` (STAC, band reads, rainfall), `processing`
  (masks, indicators, detectors, baselines), `scoring` (priority, alerts,
  retraining), `reporting` (briefs, delivery). Compose runs one worker pool per
  queue (`WORKERS_*` sizes), so a slow PDF never blocks a scene read; Flower
  shows the four queues draining independently.
- **Beat** — Tier 1 poll every 6 h, Tier 2 daily, Tier 3 weekly; rainfall daily
  at 02:00 IST; baseline rebuild monthly; ops gauges every 15 min; retraining
  attempt weekly.
- **Backfill CLI** (`app/cli.py`) — runs the whole chain in-process chunk by
  chunk and checkpoints the last completed chunk on a `jobs` row; `--resume`
  continues from the next chunk. Every stage is idempotent on (body, scene),
  so nothing is redone. Backfill alerts are never dispatched.
- **Prometheus** (`app/core/metrics.py`) — scenes ingested, scenes rejected for
  cloud (STAC vs mask), zones processed, alerts raised/updated, alerts gated by
  rainfall, task duration by stage (Celery signals), STAC failures and
  fallbacks, dispatch outcomes, plus gauges for Grafana alerting: days since
  the last usable scene per Tier 1 body, open alerts, baseline-building zones,
  validation precision. API at `/metrics`; each worker pool on `:9100`.
- **Grafana** — provisioned datasource + `infra/grafana/dashboards/jalnetra-pipeline.json`.
  **Alerting rules** in `infra/prometheus/alerts.yml`: `Tier1WaterBodyStale`
  pages when any Tier 1 body has had no usable scene for 10 days; plus STAC
  failing, slow stages, worker metrics down, dispatch failures.
- **Sentry** — set `SENTRY_DSN` (needs the `ops` extra, baked into the image);
  every task event is tagged with `water_body_id` / `scene_id` / `alert_id`.
- **Graceful degradation** — `ChainedSource` falls back to the secondary STAC
  source, counts the failure and the fallback, and `scenes.source` records who
  served each scene (`tests/test_ops.py` covers the kill-the-primary case).
- **CI** (`.github/workflows/ci.yml`) — ruff, mypy, pytest, an Alembic
  upgrade/downgrade round-trip against TimescaleDB+PostGIS, frontend
  `tsc`+`vite build`, and a backend image build.


