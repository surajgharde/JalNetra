import { format, parseISO } from "date-fns";
import type { Severity } from "@/api/types";

export const fmtDate = (iso: string) => format(parseISO(iso), "d MMM yyyy");
export const fmtDateShort = (iso: string) => format(parseISO(iso), "d MMM");
export const fmtNum = (v: number, digits = 3) =>
  Number.isFinite(v) ? v.toFixed(digits) : "—";
export const fmtPct = (v: number, digits = 1) =>
  Number.isFinite(v) ? `${v > 0 ? "+" : ""}${v.toFixed(digits)}%` : "—";
export const fmtKm2 = (v: number) => `${v.toFixed(v < 10 ? 2 : 1)} km²`;

export const INDICATOR_LABELS: Record<string, string> = {
  ndti_turbidity: "Turbidity (NDTI)",
  ndci_chlorophyll: "Chlorophyll (NDCI)",
  fai_algae: "Floating algae (FAI)",
  ndwi_extent: "Water extent (NDWI)",
  ssi_sediment: "Suspended sediment",
};
export const indicatorLabel = (key: string) => INDICATOR_LABELS[key] ?? key;

export const SEVERITY_ORDER: Severity[] = ["low", "medium", "high", "critical"];
export const severityColor: Record<Severity, string> = {
  low: "#2563eb",
  medium: "#d97706",
  high: "#ea580c",
  critical: "#b91c1c",
};
