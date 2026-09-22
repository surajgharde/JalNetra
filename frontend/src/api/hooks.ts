/**
 * TanStack Query hooks. Keys are stable arrays so a status change on an
 * alert invalidates every list that could contain it.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  AlertFilters,
  AlertStatusUpdate,
  IngestJobRequest,
  LiveImageryQuery,
  ValidationIn,
} from "./types";

export const keys = {
  health: ["health"] as const,
  bodies: (q?: object) => ["water-bodies", q ?? {}] as const,
  body: (id: string) => ["water-body", id] as const,
  observations: (id: string) => ["observations", id] as const,
  indicators: (id: string, date?: string, zone?: string) =>
    ["indicators", id, date ?? "", zone ?? ""] as const,
  series: (id: string, indicator: string, zone?: string, from?: string, to?: string) =>
    ["series", id, indicator, zone ?? "", from ?? "", to ?? ""] as const,
  alerts: (f?: AlertFilters) => ["alerts", f ?? {}] as const,
  alertsGeo: (f?: AlertFilters) => ["alerts-geo", f ?? {}] as const,
  alert: (id: string) => ["alert", id] as const,
  validations: (alertId?: string) => ["validations", alertId ?? ""] as const,
  job: (id: string) => ["job", id] as const,
  jobs: ["jobs"] as const,
  tileStyles: ["tile-styles"] as const,
  imageryStatus: ["imagery-status"] as const,
  liveImagery: (q: LiveImageryQuery) => ["live-imagery", q] as const,
};

export const useHealth = () =>
  useQuery({ queryKey: keys.health, queryFn: api.health, refetchInterval: 30_000 });

export const useWaterBodies = (q?: { district?: string; tier?: number }) =>
  useQuery({ queryKey: keys.bodies(q), queryFn: () => api.waterBodies.list({ ...q, limit: 200 }) });

export const useWaterBody = (id: string | null) =>
  useQuery({
    queryKey: keys.body(id ?? ""),
    queryFn: () => api.waterBodies.get(id!),
    enabled: !!id,
  });

export const useObservations = (id: string | null) =>
  useQuery({
    queryKey: keys.observations(id ?? ""),
    queryFn: () => api.waterBodies.observations(id!, { limit: 200 }),
    enabled: !!id,
  });

export const useIndicators = (id: string | null, date?: string, zone?: string) =>
  useQuery({
    queryKey: keys.indicators(id ?? "", date, zone),
    queryFn: () => api.waterBodies.indicators(id!, { date, zone }),
    enabled: !!id,
  });

export const useSeries = (
  id: string | null,
  indicator: string,
  zone?: string,
  from?: string,
  to?: string,
) =>
  useQuery({
    queryKey: keys.series(id ?? "", indicator, zone, from, to),
    queryFn: () => api.waterBodies.series(id!, { indicator, zone, from, to }),
    enabled: !!id,
  });

export const useAlerts = (f: AlertFilters = {}) =>
  useQuery({ queryKey: keys.alerts(f), queryFn: () => api.alerts.list(f) });

export const useAlertsGeo = (f: AlertFilters = {}) =>
  useQuery({ queryKey: keys.alertsGeo(f), queryFn: () => api.alerts.geojson(f) });

export const useAlert = (id: string | null) =>
  useQuery({ queryKey: keys.alert(id ?? ""), queryFn: () => api.alerts.get(id!), enabled: !!id });

export const useValidations = (alertId?: string) =>
  useQuery({
    queryKey: keys.validations(alertId),
    queryFn: () => api.validations.list({ alert_id: alertId }),
  });

export const useTileStyles = () =>
  useQuery({ queryKey: keys.tileStyles, queryFn: api.tiles.styles, staleTime: Infinity });

export const useImageryStatus = () =>
  useQuery({ queryKey: keys.imageryStatus, queryFn: api.imagery.status, staleTime: 5 * 60_000 });

/** A styled Earth Engine map id; the backend caches it, so the map id is stable for ~1 h. */
export const useLiveImagery = (q: LiveImageryQuery | null) =>
  useQuery({
    queryKey: keys.liveImagery(q ?? { bbox: "" }),
    queryFn: () => api.imagery.live(q!),
    enabled: !!q,
    staleTime: 30 * 60_000,
    retry: false, // a 404 (no pass in the window) or 503 (GEE off) will not change on retry
  });

export function useSetAlertStatus(alertId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AlertStatusUpdate) => api.alerts.setStatus(alertId, body),
    onSuccess: (alert) => {
      qc.setQueryData(keys.alert(alertId), alert);
      void qc.invalidateQueries({ queryKey: ["alerts"] });
      void qc.invalidateQueries({ queryKey: ["alerts-geo"] });
      void qc.invalidateQueries({ queryKey: ["water-bodies"] });
    },
  });
}

export function useSubmitValidation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ValidationIn) => api.validations.create(body),
    onSuccess: (v) => {
      void qc.invalidateQueries({ queryKey: keys.validations(v.alert_id) });
      void qc.invalidateQueries({ queryKey: keys.alert(v.alert_id) });
    },
  });
}

export function useStartIngest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: IngestJobRequest) => api.jobs.ingest(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.jobs }),
  });
}

/** Polls a job every 2 s while it is queued/running. */
export const useJob = (id: string | null) =>
  useQuery({
    queryKey: keys.job(id ?? ""),
    queryFn: () => api.jobs.get(id!),
    enabled: !!id,
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "done" || s === "failed" ? false : 2_000;
    },
  });
