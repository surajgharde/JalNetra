/**
 * UI state that several panels share: which body / date / zone is selected,
 * which raster layers are visible, which alert slide-over is open, and the
 * pipeline job being watched. Server data never lives here (TanStack Query).
 */
import { create } from "zustand";
import type { DiscoveredWaterBodyOut, LiveVisKey, RasterLayer } from "@/api/types";

/** Result of a place/coordinate search (S14): shared by the map's own search
 * box and the top bar's autocomplete, so either one can fly the map and show
 * the same discovered water bodies. */
export interface SearchResult {
  centre: [number, number]; // [lon, lat]
  radiusKm: number;
  district: string | null;
  items: DiscoveredWaterBodyOut[];
}

interface UiState {
  waterBodyId: string | null;
  date: string | null; // YYYY-MM-DD of the selected observation
  zoneId: string | null;
  indicator: string;
  layers: Record<RasterLayer, boolean>;
  showAlerts: boolean;
  showZones: boolean;
  alertId: string | null;
  /** Alert whose flagged plume the map should draw filled and zoom *to*.
   * Separate from `alertId` (the slide-over): "Show on map" closes the sheet
   * but the plume stays focused. Cleared by any manual body/date/zone change,
   * because the plume belongs to one zone on one date. */
  focusAlertId: string | null;
  /** Quick-look drawer opened from the wishlist, recent history or a map pin;
   * independent of `waterBodyId` so it never disturbs the main dashboard. */
  detailsWaterBodyId: string | null;
  jobId: string | null;
  /** Live Sentinel-2 imagery from Google Earth Engine, independent of ingestion. */
  live: { enabled: boolean; vis: LiveVisKey; composite: boolean; days: number };
  /** Active place/coordinate search result, or null when none is showing. */
  search: SearchResult | null;

  selectWaterBody: (id: string | null) => void;
  selectDate: (date: string | null) => void;
  selectZone: (id: string | null) => void;
  selectIndicator: (key: string) => void;
  toggleLayer: (layer: RasterLayer) => void;
  setShowAlerts: (v: boolean) => void;
  setShowZones: (v: boolean) => void;
  openAlert: (id: string | null) => void;
  /** Focus one alert's plume on the map, selecting its body/date/zone in one
   * commit so `selectWaterBody`'s date/zone reset cannot race it. Leaves
   * `alertId` alone: the caller decides whether the slide-over opens or closes. */
  focusAlert: (
    a: { alertId: string; waterBodyId: string; date: string; zoneId: string } | null,
  ) => void;
  openDetails: (id: string | null) => void;
  watchJob: (id: string | null) => void;
  setLive: (patch: Partial<UiState["live"]>) => void;
  setSearchResult: (result: SearchResult | null) => void;
}

export const useUi = create<UiState>((set) => ({
  waterBodyId: null,
  date: null,
  zoneId: null,
  indicator: "ndti_turbidity",
  layers: {
    watermask: false,
    ndti_turbidity: true,
    ndci_chlorophyll: false,
    fai_algal: false,
    sediment_proxy: false,
    mndwi_extent: false,
    anomaly: false,
  },
  showAlerts: true,
  showZones: true,
  alertId: null,
  focusAlertId: null,
  detailsWaterBodyId: null,
  jobId: null,
  live: { enabled: false, vis: "truecolor", composite: false, days: 30 },
  search: null,

  selectWaterBody: (id) => set({ waterBodyId: id, date: null, zoneId: null, focusAlertId: null }),
  selectDate: (date) => set({ date, focusAlertId: null }),
  selectZone: (zoneId) => set({ zoneId, focusAlertId: null }),
  selectIndicator: (indicator) => set({ indicator }),
  toggleLayer: (layer) =>
    set((s) => ({ layers: { ...s.layers, [layer]: !s.layers[layer] } })),
  setShowAlerts: (showAlerts) => set({ showAlerts }),
  setShowZones: (showZones) => set({ showZones }),
  openAlert: (alertId) => set({ alertId }),
  focusAlert: (a) =>
    set(
      a === null
        ? { focusAlertId: null }
        : {
            focusAlertId: a.alertId,
            waterBodyId: a.waterBodyId,
            date: a.date,
            zoneId: a.zoneId,
          },
    ),
  openDetails: (detailsWaterBodyId) => set({ detailsWaterBodyId }),
  watchJob: (jobId) => set({ jobId }),
  setLive: (patch) => set((s) => ({ live: { ...s.live, ...patch } })),
  setSearchResult: (search) => set({ search }),
}));
