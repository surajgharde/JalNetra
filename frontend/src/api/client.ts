/**
 * Typed API client — the only module allowed to touch the transport.
 * Paths mirror the frozen contract table one-to-one.
 */
import { ApiError, buildUrl, request } from "./http";
import type {
  AlertFeatureCollection,
  AlertFilters,
  AlertList,
  AlertOut,
  AlertStatusUpdate,
  HealthResponse,
  ImageryStatus,
  IndicatorsResponse,
  IngestJobRequest,
  JobList,
  JobOut,
  LiveImagery,
  LiveImageryQuery,
  ObservationList,
  RasterLayer,
  SeriesResponse,
  TileStyle,
  ValidationIn,
  ValidationList,
  ValidationOut,
  ValidationSummary,
  WaterBodyDetail,
  WaterBodyList,
} from "./types";

const V1 = "/api/v1";

export const api = {
  health: () => request<HealthResponse>("/health"),

  waterBodies: {
    list: (q?: { district?: string; tier?: number; limit?: number; cursor?: string }) =>
      request<WaterBodyList>(`${V1}/water-bodies`, { query: q }),
    get: (id: string) => request<WaterBodyDetail>(`${V1}/water-bodies/${id}`),
    observations: (id: string, q?: { from?: string; to?: string; limit?: number; cursor?: string }) =>
      request<ObservationList>(`${V1}/water-bodies/${id}/observations`, { query: q }),
    indicators: (id: string, q?: { date?: string; zone?: string }) =>
      request<IndicatorsResponse>(`${V1}/water-bodies/${id}/indicators`, { query: q }),
    series: (id: string, q: { indicator: string; zone?: string; from?: string; to?: string }) =>
      request<SeriesResponse>(`${V1}/water-bodies/${id}/series`, { query: q }),
  },

  alerts: {
    list: (f: AlertFilters = {}) => request<AlertList>(`${V1}/alerts`, { query: { ...f } }),
    get: (id: string) => request<AlertOut>(`${V1}/alerts/${id}`),
    geojson: (f: AlertFilters = {}) =>
      request<AlertFeatureCollection>(`${V1}/alerts.geojson`, { query: { ...f } }),
    setStatus: (id: string, body: AlertStatusUpdate) =>
      request<AlertOut>(`${V1}/alerts/${id}/status`, { method: "PATCH", body }),
    briefUrl: (id: string) => `${V1}/alerts/${id}/brief.pdf`,
    geometryUrl: (id: string) => `${V1}/alerts/${id}/geometry.geojson`,
  },

  validations: {
    list: (q?: { alert_id?: string; limit?: number; cursor?: string }) =>
      request<ValidationList>(`${V1}/validations`, { query: q }),
    create: (body: ValidationIn) =>
      request<ValidationOut>(`${V1}/validations`, { method: "POST", body }),
    summary: () => request<ValidationSummary>(`${V1}/validations/summary`),
    /** Multipart upload; the only place the transport is bypassed, because JSON cannot carry a file. */
    uploadPhoto: async (id: number, file: File): Promise<ValidationOut> => {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`${V1}/validations/${id}/photo`, { method: "POST", body: form });
      if (!res.ok) throw new ApiError(res.status, (await res.text()) || res.statusText);
      return (await res.json()) as ValidationOut;
    },
  },

  jobs: {
    ingest: (body: IngestJobRequest) =>
      request<JobOut>(`${V1}/jobs/ingest`, { method: "POST", body }),
    get: (id: string) => request<JobOut>(`${V1}/jobs/${id}`),
    list: (q?: { water_body_id?: string; limit?: number }) =>
      request<JobList>(`${V1}/jobs`, { query: q }),
  },

  /** Live Sentinel-2 imagery rendered by Google Earth Engine (no ingestion involved). */
  imagery: {
    status: () => request<ImageryStatus>(`${V1}/imagery/status`),
    live: (q: LiveImageryQuery) => request<LiveImagery>(`${V1}/imagery/live`, { query: { ...q } }),
  },

  tiles: {
    styles: () => request<Record<string, TileStyle>>("/tiles/styles"),
    /** Leaflet URL template for L.tileLayer: body-scope chip of a layer on a date. */
    template: (layer: RasterLayer, waterBodyId: string, date: string) =>
      `/tiles/${layer}/${encodeURIComponent(waterBodyId)}/${date}/{z}/{x}/{y}.png`,
    /** Turn a `/tiles/chip/...{z}/{x}/{y}.png` evidence URL into a Leaflet template (identity). */
    fromEvidence: (url: string) => buildUrl(url).replace(/%7B/g, "{").replace(/%7D/g, "}"),
  },
};
