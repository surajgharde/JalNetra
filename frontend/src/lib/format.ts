import { format, parseISO } from "date-fns";
import type { LiveVisKey, RasterLayer, Severity } from "@/api/types";

export const fmtDate = (iso: string) => format(parseISO(iso), "d MMM yyyy");
export const fmtDateShort = (iso: string) => format(parseISO(iso), "d MMM");
export const fmtDateTime = (iso: string) => format(parseISO(iso), "d MMM yyyy HH:mm");
export const fmtNum = (v: number | null | undefined, digits = 3) =>
  v === null || v === undefined || !Number.isFinite(v) ? "—" : v.toFixed(digits);
export const fmtSigned = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || !Number.isFinite(v)
    ? "—"
    : `${v > 0 ? "+" : ""}${v.toFixed(digits)}`;
export const fmtPct = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined || !Number.isFinite(v)
    ? "—"
    : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
export const fmtKm2 = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v.toFixed(v < 10 ? 2 : 1)} km²`;

/** Display names mirror the backend indicator registry (S4). */
export const INDICATOR_LABELS: Record<string, string> = {
  ndti_turbidity: "Turbidity (NDTI)",
  ndci_chlorophyll: "Chlorophyll-a (NDCI)",
  fai_algal: "Floating algae (FAI)",
  sediment_proxy: "Suspended sediment",
  mndwi_extent: "Water extent (MNDWI)",
};
export const QUALITY_INDICATORS = [
  "ndti_turbidity",
  "ndci_chlorophyll",
  "fai_algal",
  "sediment_proxy",
] as const;
export const indicatorLabel = (key: string) => INDICATOR_LABELS[key] ?? key;
/** Same label without the index in brackets, for tabs and other tight rows. */
export const indicatorShortLabel = (key: string) => indicatorLabel(key).replace(/\s*\(.*\)$/, "");

export const RASTER_LAYERS: { key: RasterLayer; label: string; swatch: string }[] = [
  { key: "ndti_turbidity", label: "Turbidity", swatch: "linear-gradient(90deg,#fff7bc,#fe9929,#993404)" },
  { key: "ndci_chlorophyll", label: "Chlorophyll", swatch: "linear-gradient(90deg,#f7fcf5,#74c476,#00441b)" },
  { key: "fai_algal", label: "Floating algae", swatch: "linear-gradient(90deg,#ffffe5,#addd8e,#004529)" },
  { key: "sediment_proxy", label: "Sediment", swatch: "linear-gradient(90deg,#fff5eb,#fd8d3c,#7f2704)" },
  { key: "mndwi_extent", label: "Water extent", swatch: "linear-gradient(90deg,#f7fbff,#6baed6,#08306b)" },
  { key: "watermask", label: "Water mask", swatch: "#1f77b4" },
];

/** Live Earth Engine visualisations (keys mirror the backend's LIVE_VIS). */
export const LIVE_VIS_OPTIONS: { key: LiveVisKey; label: string; swatch: string }[] = [
  { key: "truecolor", label: "True colour", swatch: "linear-gradient(90deg,#1e3a5f,#6b8e23,#d9c27e)" },
  { key: "falsecolor", label: "False colour (NIR)", swatch: "linear-gradient(90deg,#1a1a4d,#c0392b,#f5b7b1)" },
  { key: "ndti", label: "Turbidity (NDTI)", swatch: "linear-gradient(90deg,#fff7bc,#fe9929,#993404)" },
  { key: "ndci", label: "Chlorophyll (NDCI)", swatch: "linear-gradient(90deg,#f7fcf5,#74c476,#00441b)" },
  { key: "mndwi", label: "Water extent (MNDWI)", swatch: "linear-gradient(90deg,#f7fbff,#6baed6,#08306b)" },
];
/** Whole-state view when no water body is selected (Maharashtra, lon/lat). */
export const MAHARASHTRA_BBOX: [number, number, number, number] = [72.6, 15.6, 80.9, 22.1];

export const SEVERITY_ORDER: Severity[] = ["low", "medium", "high"];
export const severityColor: Record<Severity, string> = {
  low: "#2563eb",
  medium: "#d97706",
  high: "#b91c1c",
};
export const severityBg: Record<Severity, string> = {
  low: "bg-blue-100 text-blue-800",
  medium: "bg-amber-100 text-amber-800",
  high: "bg-red-100 text-red-800",
};
export const statusLabel: Record<string, string> = {
  open: "Open",
  investigating: "Investigating",
  validated: "Validated",
  dismissed: "Dismissed",
  normal: "Normal",
  watch: "Watch",
  alert: "Alert",
  baseline_building: "Baseline building",
  no_data: "No data",
};
export const STAGE_LABELS: Record<string, string> = {
  ingestion: "Fetching Sentinel-2 bands",
  mask: "Detecting water",
  indicators: "Computing indicators",
  anomalies: "Running detectors",
  scoring: "Scoring & explaining",
};
