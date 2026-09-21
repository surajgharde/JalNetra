/**
 * UI state that several panels share: which body / date / zone is selected,
 * which raster layers are visible, which alert slide-over is open, and the
 * pipeline job being watched. Server data never lives here (TanStack Query).
 */
import { create } from "zustand";
import type { RasterLayer } from "@/api/types";

interface UiState {
  waterBodyId: string | null;
  date: string | null; // YYYY-MM-DD of the selected observation
  zoneId: string | null;
  indicator: string;
  layers: Record<RasterLayer, boolean>;
  showAlerts: boolean;
  showZones: boolean;
  alertId: string | null;
  jobId: string | null;

  selectWaterBody: (id: string | null) => void;
  selectDate: (date: string | null) => void;
  selectZone: (id: string | null) => void;
  selectIndicator: (key: string) => void;
  toggleLayer: (layer: RasterLayer) => void;
  setShowAlerts: (v: boolean) => void;
  setShowZones: (v: boolean) => void;
  openAlert: (id: string | null) => void;
  watchJob: (id: string | null) => void;
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
  jobId: null,

  selectWaterBody: (id) => set({ waterBodyId: id, date: null, zoneId: null }),
  selectDate: (date) => set({ date }),
  selectZone: (zoneId) => set({ zoneId }),
  selectIndicator: (indicator) => set({ indicator }),
  toggleLayer: (layer) =>
    set((s) => ({ layers: { ...s.layers, [layer]: !s.layers[layer] } })),
  setShowAlerts: (showAlerts) => set({ showAlerts }),
  setShowZones: (showZones) => set({ showZones }),
  openAlert: (alertId) => set({ alertId }),
  watchJob: (jobId) => set({ jobId }),
}));
