/**
 * Frozen API contract, as generated from the backend's OpenAPI schema
 * (`npm run gen:api` after exporting /openapi.json to ./openapi.json).
 * Nothing here is hand-written: every name is an alias onto the generated
 * `components["schemas"]`, so a backend rename is a compile error, not a
 * silent blank panel.
 */
import type { components } from "./schema";

type S = components["schemas"];

export type Position = [lon: number, lat: number];

export type HealthResponse = S["HealthResponse"];

export type WaterBodyListItem = S["WaterBodyListItem"];
export type WaterBodyList = S["WaterBodyList"];
export type WaterBodyDetail = S["WaterBodyDetail"];
export type LatestObservation = S["LatestObservation"];
export type ZoneFeatureCollection = S["FeatureCollection"];
export type ZoneFeature = S["Feature"];
export interface ZoneFeatureProps {
  id: string;
  name: string;
  seq: number;
  area_km2: number;
  baseline_status: "usable" | "building" | "none";
  open_alert_id: string | null;
}

export type ObservationItem = S["ObservationItem"];
export type ObservationList = S["ObservationList"];

export type IndicatorsResponse = S["IndicatorsResponse"];
export type ZoneIndicators = S["ZoneIndicators"];
export type ZoneIndicator = S["ZoneIndicator"];

export type SeriesResponse = S["SeriesResponse"];
export type SeriesPoint = S["SeriesPoint"];

export type AlertOut = S["AlertOut"];
export type AlertList = S["AlertList"];
export type AlertListItem = S["AlertListItem"];
export type AlertStatusUpdate = S["AlertStatusUpdate"];
export type ContributionOut = S["ContributionOut"];
export type IndicatorReading = S["IndicatorReading"];
export type TimelineEntry = S["TimelineEntry"];
export type AlertFeatureCollection = S["GeoJSONFeatureCollection"];
export type AlertFeature = S["GeoJSONFeature"];

export type ValidationIn = S["ValidationIn"];
export type ValidationOut = S["ValidationOut"];
export type ValidationList = S["ValidationList"];
export type LabResults = S["LabResults"];
export type ValidationSummary = S["ValidationSummary"];

export type Methodology = S["Methodology"];
export type MethodologyStep = S["Step"];
export type LiveImagery = S["LiveImageryOut"];
export type ImageryStatus = S["ImageryStatus"];
export type LiveVis = S["LiveVisOut"];

export type IngestJobRequest = S["IngestJobRequest"];
export type JobOut = S["JobOut"];
export type JobList = S["JobList"];
export type StageProgress = S["StageProgress"];

export type Severity = AlertOut["severity"];
export type AlertStatus = AlertOut["status"];
export type BodyStatus = WaterBodyListItem["status"];
export type JobStatus = JobOut["status"];
export type Verdict = NonNullable<ValidationOut["verdict"]>;
export type LiveVisKey = "truecolor" | "falsecolor" | "ndti" | "ndci" | "mndwi";

/** Query parameters accepted by GET /api/v1/imagery/live. */
export interface LiveImageryQuery {
  bbox: string; // minx,miny,maxx,maxy
  vis?: LiveVisKey;
  date?: string;
  days?: number;
  max_cloud?: number;
  composite?: boolean;
}

/** Query parameters accepted by GET /api/v1/alerts and /alerts.geojson. */
export interface AlertFilters {
  status?: "open" | "investigating" | "validated" | "dismissed" | "active" | "all";
  severity?: Severity;
  min_priority?: number;
  district?: string;
  water_body_id?: string;
  limit?: number;
  offset?: number;
}

/** Raster layers the tile proxy styles (see /tiles/styles). */
export type RasterLayer =
  | "watermask"
  | "ndti_turbidity"
  | "ndci_chlorophyll"
  | "fai_algal"
  | "sediment_proxy"
  | "mndwi_extent"
  | "anomaly";

export type TileStyle = {
  colormap_name: string | null;
  rescale: [number, number] | null;
  colormap: Record<string, number[]> | null;
  nodata: number | null;
};
