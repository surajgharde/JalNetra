/**
 * Frozen API contract (plan section "Internal API contract").
 * Field names here must match the FastAPI Pydantic schemas exactly.
 * When S9 lands, regenerate from /openapi.json and diff against this file.
 */

export type Position = [lon: number, lat: number];

export interface GeoJSONPolygon {
  type: "Polygon";
  coordinates: Position[][];
}
export interface GeoJSONMultiPolygon {
  type: "MultiPolygon";
  coordinates: Position[][][];
}
export type Geometry = GeoJSONPolygon | GeoJSONMultiPolygon;

export interface Feature<P = Record<string, unknown>, G = Geometry> {
  type: "Feature";
  id?: string;
  geometry: G;
  properties: P;
}
export interface FeatureCollection<P = Record<string, unknown>, G = Geometry> {
  type: "FeatureCollection";
  features: Feature<P, G>[];
}

export type Tier = 1 | 2 | 3;
export type WaterBodyKind = "reservoir" | "lake" | "river_stretch";
export type Severity = "low" | "medium" | "high" | "critical";
export type AlertStatus = "open" | "investigating" | "validated" | "dismissed";
export type Verdict = "matched" | "not_matched" | "inconclusive";
export type BodyStatus = "normal" | "watch" | "alert" | "no_data";

export interface WaterBodyRef {
  id: string;
  name: string;
  district: string;
}

export interface ZoneRef {
  id: string;
  name: string;
  centroid: Position;
}

export interface LatestObservation {
  scene_id: string;
  observed_on: string;
  cloud_cover_pct: number;
  usable: boolean;
}

export interface WaterBodySummary {
  id: string;
  name: string;
  district: string;
  kind: WaterBodyKind;
  tier: Tier;
  area_km2: number;
  centroid: Position;
  status: BodyStatus;
  open_alerts: number;
  max_priority: number | null;
  latest_observation: LatestObservation | null;
}

export interface Zone {
  id: string;
  name: string;
  area_km2: number;
  centroid: Position;
  geometry: Geometry;
}

export interface WaterBodyDetail extends WaterBodySummary {
  drinking_water: boolean;
  urban: boolean;
  bbox: [number, number, number, number];
  boundary: Geometry;
  zones: Zone[];
}

export interface Observation {
  scene_id: string;
  observed_on: string;
  cloud_cover_pct: number;
  usable: boolean;
  valid_pixel_pct: number;
}

export interface IndicatorValue {
  key: string;
  value: number;
  baseline_mean: number;
  baseline_std: number;
  z_score: number;
  deviation_pct: number;
}

export interface IndicatorResponse {
  water_body_id: string;
  zone_id: string | null;
  observed_on: string;
  scene_id: string;
  indicators: IndicatorValue[];
}

export interface SeriesPoint {
  observed_on: string;
  value: number;
  baseline_mean: number;
  baseline_p10: number;
  baseline_p90: number;
  usable: boolean;
}

export interface SeriesResponse {
  water_body_id: string;
  zone_id: string | null;
  indicator: string;
  points: SeriesPoint[];
}

export interface Contribution {
  factor: string;
  value: number;
}

export interface Alert {
  alert_id: string;
  water_body: WaterBodyRef;
  zone: ZoneRef;
  observed_on: string;
  affected_area_km2: number;
  primary_indicator: string;
  severity: Severity;
  confidence: number;
  priority_score: number;
  status: AlertStatus;
  indicators: IndicatorValue[];
  explanation: {
    summary: string;
    contributions: Contribution[];
  };
  context: {
    rainfall_72h_mm: number;
    cloud_cover_pct: number;
    natural_cause_likely: boolean;
  };
  evidence: {
    baseline_composite_url: string;
    current_observation_url: string;
    anomaly_mask_url: string;
  };
  /** Always present. The frontend renders it verbatim; never hardcode. */
  disclaimer: string;
}

export interface AlertFilters {
  status?: AlertStatus;
  severity?: Severity;
  min_priority?: number;
  district?: string;
  water_body_id?: string;
  from?: string;
  to?: string;
  cursor?: string;
  limit?: number;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  total: number;
}

export interface AlertFeatureProps {
  alert_id: string;
  water_body_id: string;
  zone_id: string;
  severity: Severity;
  priority_score: number;
  status: AlertStatus;
  observed_on: string;
  primary_indicator: string;
}

export interface LabResults {
  turbidity_ntu?: number | null;
  ph?: number | null;
  dissolved_oxygen_mg_l?: number | null;
  chlorophyll_a_ug_l?: number | null;
  bod_mg_l?: number | null;
  notes?: string | null;
}

export interface ValidationCreate {
  alert_id: string;
  sampled_on: string;
  submitted_by: string;
  lab_results: LabResults;
}

export interface Validation extends ValidationCreate {
  id: string;
  verdict: Verdict;
  verdict_reason: string;
  submitted_at: string;
}

export interface IngestJobCreate {
  water_body_id: string;
  from: string;
  to: string;
}

export type JobState = "queued" | "running" | "succeeded" | "failed";

export interface JobStage {
  key: string;
  label: string;
  state: "pending" | "running" | "done" | "failed";
  detail?: string | null;
}

export interface Job {
  job_id: string;
  water_body_id: string;
  state: JobState;
  progress_pct: number;
  current_stage: string;
  stages: JobStage[];
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface HealthCheck {
  name: string;
  ok: boolean;
  detail?: string | null;
  latency_ms?: number | null;
}
export interface Health {
  status: "ok" | "degraded" | "down";
  checks: HealthCheck[];
}

export type RasterLayer = "truecolor" | "watermask" | "ndti" | "ndci" | "fai" | "anomaly";
