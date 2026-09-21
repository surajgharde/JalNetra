/**
 * Typed API client — the only module allowed to touch the transport.
 * Paths mirror the frozen contract table one-to-one.
 */
import { buildUrl, request } from "./http";
import type {
  Alert,
  AlertFeatureProps,
  AlertFilters,
  FeatureCollection,
  GeoJSONPolygon,
  Health,
  IndicatorResponse,
  IngestJobCreate,
  Job,
  Observation,
  Page,
  RasterLayer,
  SeriesResponse,
  Validation,
  ValidationCreate,
  WaterBodyDetail,
  WaterBodySummary,
} from "./types";

const V1 = "/api/v1";

export const api = {
  health: () => request<Health>("/health"),

  waterBodies: {
    list: (q?: { district?: string; tier?: number; q?: string }) =>
      request<WaterBodySummary[]>(`${V1}/water-bodies`, { query: q }),
    get: (id: string) => request<WaterBodyDetail>(`${V1}/water-bodies/${id}`),
    observations: (id: string, q?: { from?: string; to?: string }) =>
      request<Observation[]>(`${V1}/water-bodies/${id}/observations`, { query: q }),
    indicators: (id: string, q: { date?: string; zone?: string }) =>
      request<IndicatorResponse>(`${V1}/water-bodies/${id}/indicators`, { query: q }),
    series: (id: string, q: { indicator: string; zone?: string; from?: string; to?: string }) =>
      request<SeriesResponse>(`${V1}/water-bodies/${id}/series`, { query: q }),
  },

  alerts: {
    list: (f: AlertFilters = {}) => request<Page<Alert>>(`${V1}/alerts`, { query: f }),
    get: (id: string) => request<Alert>(`${V1}/alerts/${id}`),
    geojson: (f: Pick<AlertFilters, "status" | "water_body_id"> = {}) =>
      request<FeatureCollection<AlertFeatureProps, GeoJSONPolygon>>(`${V1}/alerts.geojson`, {
        query: f,
      }),
    briefUrl: (id: string) => `${V1}/alerts/${id}/brief.pdf`,
  },

  validations: {
    list: (q?: { alert_id?: string }) => request<Validation[]>(`${V1}/validations`, { query: q }),
    create: (body: ValidationCreate) =>
      request<Validation>(`${V1}/validations`, { method: "POST", body }),
  },

  jobs: {
    ingest: (body: IngestJobCreate) => request<Job>(`${V1}/jobs/ingest`, { method: "POST", body }),
    get: (id: string) => request<Job>(`${V1}/jobs/${id}`),
  },

  tiles: {
    /** Leaflet URL template for L.tileLayer; braces must survive encoding. */
    template: (
      layer: RasterLayer,
      q?: { water_body_id?: string; date?: string; scene_id?: string },
    ) => buildUrl(`/tiles/${layer}/{z}/{x}/{y}.png`, q).replace(/%7B/g, "{").replace(/%7D/g, "}"),
  },
};
