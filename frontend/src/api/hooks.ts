/**
 * TanStack Query hooks. Keys are stable arrays so a status change on an
 * alert invalidates every list that could contain it.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  AlertFilters,
  AlertStatusUpdate,
  DiscoverAtPointRequest,
  ImportDynamicRequest,
  IngestJobRequest,
  LiveImageryQuery,
  SearchAndDiscoverRequest,
  ValidationIn,
  WishlistItemIn,
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
  wishlist: ["wishlist"] as const,
  recent: (limit?: number) => ["recent-history", limit ?? null] as const,
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


// --- Wishlist & recent history (S14) -----------------------------------------------

/** The saved-water-bodies sidebar list; each row already carries live status. */
export const useWishlist = () =>
  useQuery({ queryKey: keys.wishlist, queryFn: api.wishlist.list, staleTime: 30_000 });

export function useAddToWishlist() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: WishlistItemIn) => api.wishlist.add(body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.wishlist }),
  });
}

export function useRemoveFromWishlist() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (itemId: number) => api.wishlist.remove(itemId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.wishlist }),
  });
}

/** The last few water bodies inspected; the sidebar shows this without polling. */
export const useRecentHistory = (limit?: number) =>
  useQuery({ queryKey: keys.recent(limit), queryFn: () => api.history.recent(limit), staleTime: 15_000 });

/**
 * Records a water-body view. Fire-and-forget from the caller's point of view —
 * failures are swallowed (a missed history entry is not worth surfacing an
 * error for) but the recent list is still refreshed on success so a newly
 * viewed body appears without a manual reload.
 */
export function useTouchRecent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (waterBodyId: string) => api.history.touch(waterBodyId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["recent-history"] }),
    onError: () => {
      /* best-effort: a failed history write should not surface to the user */
    },
  });
}

// --- Geographic search & discovery (S14) --------------------------------------------

/** Triggered by the map's "Identify water bodies" action, not on every keystroke. */
export function useSearchAndDiscover() {
  return useMutation({
    mutationFn: (body: SearchAndDiscoverRequest) => api.discovery.searchAndDiscover(body),
  });
}

/** Debounced, India-only autocomplete for the search bar; `null` disables the query. */
export function usePlaceSuggestions(query: string | null) {
  return useQuery({
    queryKey: ["place-suggestions", query ?? ""],
    queryFn: () => api.discovery.placeSuggestions(query!),
    enabled: !!query,
    staleTime: 60_000,
  });
}

/** Triggered by picking an autocomplete suggestion: scans straight from its lat/lon. */
export function useDiscoverAtPoint() {
  return useMutation({
    mutationFn: (body: DiscoverAtPointRequest) => api.discovery.discoverAtPoint(body),
  });
}

export function useImportDynamic() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ImportDynamicRequest) => api.discovery.importDynamic(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["water-bodies"] });
      void qc.invalidateQueries({ queryKey: keys.wishlist });
    },
  });
}
