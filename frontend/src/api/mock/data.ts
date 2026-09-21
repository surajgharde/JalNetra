/**
 * Deterministic fixtures for the mock transport. Everything derives from a
 * seeded PRNG keyed on ids, so the demo looks identical on every reload.
 * Water body geometry comes from the real S1 registry seed (simplified).
 */
import { addDays, formatISO, parseISO, subDays } from "date-fns";
import bodiesJson from "./water_bodies.json";
import type {
  Alert,
  AlertStatus,
  BodyStatus,
  Geometry,
  IndicatorValue,
  Observation,
  Position,
  SeriesPoint,
  Severity,
  WaterBodyDetail,
  WaterBodySummary,
  Zone,
} from "../types";

// ---------- PRNG ----------
function hash(s: string): number {
  let h = 1779033703 ^ s.length;
  for (let i = 0; i < s.length; i++) {
    h = Math.imul(h ^ s.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  return h >>> 0;
}
export function rng(seed: string) {
  let a = hash(seed);
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const gauss = (r: () => number) => {
  const u = 1 - r();
  const v = r();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
};

// ---------- Time ----------
export const TODAY = "2026-09-20";
const today = parseISO(TODAY);
export const isoDay = (d: Date) => formatISO(d, { representation: "date" });

// ---------- Registry ----------
interface RawBody {
  id: string;
  name: string;
  district: string;
  kind: WaterBodyDetail["kind"];
  tier: WaterBodyDetail["tier"];
  drinking_water: boolean;
  urban: boolean;
  area_km2: number;
  centroid: Position;
  bbox: [number, number, number, number];
  geometry: Geometry;
  zones: Zone[];
}
const RAW = bodiesJson as unknown as RawBody[];

export const DISCLAIMER =
  "Satellite-observed anomaly. Ground and laboratory testing recommended for validation.";

export const INDICATORS = ["ndti_turbidity", "ndci_chlorophyll", "fai_algae", "ndwi_extent"] as const;
type IndicatorKey = (typeof INDICATORS)[number];

const BASE: Record<IndicatorKey, { mean: number; std: number }> = {
  ndti_turbidity: { mean: 0.128, std: 0.021 },
  ndci_chlorophyll: { mean: 0.064, std: 0.018 },
  fai_algae: { mean: -0.012, std: 0.009 },
  ndwi_extent: { mean: 0.41, std: 0.05 },
};

// ---------- Observations: Sentinel-2 revisit ≈ 5 days, monsoon cloud gap ----------
export function observationsFor(wbId: string): Observation[] {
  const r = rng(`obs:${wbId}`);
  const out: Observation[] = [];
  // Phase the 5-day cadence per MGRS tile so bodies don't all share dates.
  let d = subDays(today, 3 + Math.floor(r() * 5));
  for (let i = 0; i < 40; i++) {
    const month = d.getMonth() + 1;
    const monsoon = month >= 6 && month <= 8; // Jun–Aug: heavy cloud
    const cloud = monsoon ? 40 + r() * 58 : month === 9 ? 5 + r() * 45 : r() * 18;
    const usable = cloud < 30;
    out.push({
      scene_id: `S2_${wbId.replace("wb_", "").toUpperCase().slice(0, 6)}_${isoDay(d).replace(/-/g, "")}`,
      observed_on: isoDay(d),
      cloud_cover_pct: +cloud.toFixed(1),
      usable,
      valid_pixel_pct: usable ? +(70 + r() * 29).toFixed(1) : +(r() * 40).toFixed(1),
    });
    d = subDays(d, 5);
  }
  return out.reverse();
}

// ---------- Anomaly script: which zones deviate, when, how much ----------
interface Episode {
  wb: string;
  zone: number;
  indicator: IndicatorKey;
  start: string;
  peak: number; // multiplier over baseline at peak
  days: number;
  severity: Severity;
  status: AlertStatus;
  rain72: number;
}
const EPISODES: Episode[] = [
  { wb: "wb_khadakwasla", zone: 3, indicator: "ndti_turbidity", start: "2026-09-08", peak: 2.44, days: 14, severity: "high", status: "open", rain72: 4.2 },
  { wb: "wb_mula_mutha", zone: 2, indicator: "ndci_chlorophyll", start: "2026-09-01", peak: 3.1, days: 25, severity: "critical", status: "investigating", rain72: 1.8 },
  { wb: "wb_pashan_lake", zone: 1, indicator: "fai_algae", start: "2026-09-05", peak: 4.0, days: 20, severity: "high", status: "open", rain72: 0.0 },
  { wb: "wb_indrayani_alandi", zone: 1, indicator: "ndti_turbidity", start: "2026-09-10", peak: 1.9, days: 12, severity: "medium", status: "open", rain72: 22.6 },
  { wb: "wb_pavana", zone: 5, indicator: "ndti_turbidity", start: "2026-09-12", peak: 1.6, days: 10, severity: "medium", status: "open", rain72: 31.4 },
  { wb: "wb_katraj_lake", zone: 2, indicator: "ndci_chlorophyll", start: "2026-08-28", peak: 2.6, days: 30, severity: "high", status: "validated", rain72: 3.1 },
  { wb: "wb_bhatghar", zone: 7, indicator: "ndti_turbidity", start: "2026-09-14", peak: 1.45, days: 8, severity: "low", status: "open", rain72: 12.0 },
  { wb: "wb_ghod", zone: 2, indicator: "fai_algae", start: "2026-09-03", peak: 2.2, days: 18, severity: "medium", status: "dismissed", rain72: 0.4 },
  { wb: "wb_nazare", zone: 3, indicator: "ndti_turbidity", start: "2026-09-15", peak: 1.7, days: 9, severity: "medium", status: "open", rain72: 6.5 },
  { wb: "wb_jambhulwadi", zone: 1, indicator: "ndci_chlorophyll", start: "2026-09-11", peak: 2.0, days: 14, severity: "medium", status: "open", rain72: 2.2 },
  { wb: "wb_ujani", zone: 4, indicator: "ndti_turbidity", start: "2026-09-16", peak: 1.35, days: 6, severity: "low", status: "open", rain72: 9.9 },
  { wb: "wb_yedgaon", zone: 3, indicator: "ndti_turbidity", start: "2026-09-13", peak: 1.5, days: 10, severity: "low", status: "open", rain72: 18.3 },
];

function episodeFactor(ep: Episode, date: string): number {
  const t = (parseISO(date).getTime() - parseISO(ep.start).getTime()) / 86400000;
  if (t < 0 || t > ep.days) return 1;
  const x = t / ep.days; // rises quickly, decays slowly
  const shape = x < 0.35 ? x / 0.35 : 1 - ((x - 0.35) / 0.65) * 0.7;
  return 1 + (ep.peak - 1) * shape;
}

function indicatorValue(wbId: string, zoneId: string | null, key: IndicatorKey, date: string): IndicatorValue {
  const base = BASE[key];
  const r = rng(`ind:${wbId}:${zoneId ?? "all"}:${key}:${date}`);
  const seasonal = 1 + 0.08 * Math.sin((parseISO(date).getDate() / 31) * Math.PI);
  let factor = seasonal;
  for (const ep of EPISODES) {
    if (ep.wb !== wbId || ep.indicator !== key) continue;
    const epZone = `${ep.wb}_z${ep.zone}`;
    const f = episodeFactor(ep, date);
    if (zoneId === epZone) factor *= f;
    else if (zoneId === null) factor *= 1 + (f - 1) * 0.35; // body-wide mean is diluted
  }
  const value = base.mean * factor + gauss(r) * base.std * 0.6;
  const z = (value - base.mean) / base.std;
  return {
    key,
    value: +value.toFixed(3),
    baseline_mean: base.mean,
    baseline_std: base.std,
    z_score: +z.toFixed(2),
    deviation_pct: +(((value - base.mean) / Math.abs(base.mean)) * 100).toFixed(1),
  };
}

export function indicatorsFor(wbId: string, zoneId: string | null, date: string): IndicatorValue[] {
  return INDICATORS.map((k) => indicatorValue(wbId, zoneId, k, date));
}

export function seriesFor(wbId: string, zoneId: string | null, key: string, from?: string, to?: string): SeriesPoint[] {
  const ik = (INDICATORS as readonly string[]).includes(key) ? (key as IndicatorKey) : "ndti_turbidity";
  const base = BASE[ik];
  return observationsFor(wbId)
    .filter((o) => (!from || o.observed_on >= from) && (!to || o.observed_on <= to))
    .map((o) => {
      const v = indicatorValue(wbId, zoneId, ik, o.observed_on);
      const doy = parseISO(o.observed_on).getDate();
      const drift = 1 + 0.06 * Math.sin((doy / 31) * Math.PI);
      return {
        observed_on: o.observed_on,
        value: o.usable ? v.value : +(v.value + gauss(rng(`cloud:${o.scene_id}`)) * base.std * 2.5).toFixed(3),
        baseline_mean: +(base.mean * drift).toFixed(3),
        baseline_p10: +(base.mean * drift - 1.28 * base.std).toFixed(3),
        baseline_p90: +(base.mean * drift + 1.28 * base.std).toFixed(3),
        usable: o.usable,
      };
    });
}

// ---------- Alerts ----------
const severityFloor: Record<Severity, number> = { low: 25, medium: 45, high: 65, critical: 85 };

function buildAlert(ep: Episode): Alert | null {
  const wb = RAW.find((b) => b.id === ep.wb);
  if (!wb) return null;
  const zone = wb.zones[ep.zone - 1] ?? wb.zones[0];
  const obs = observationsFor(wb.id).filter((o) => o.usable && o.observed_on >= ep.start);
  const peakDay = isoDay(addDays(parseISO(ep.start), Math.round(ep.days * 0.35)));
  const observed = obs.find((o) => o.observed_on >= peakDay) ?? obs[obs.length - 1];
  if (!observed) return null;
  const observedOn = wb.id === "wb_khadakwasla" ? "2026-09-17" : observed.observed_on;
  const r = rng(`alert:${wb.id}:${zone.id}`);
  const primary = indicatorValue(wb.id, zone.id, ep.indicator, observedOn);
  const others = INDICATORS.filter((k) => k !== ep.indicator).map((k) => indicatorValue(wb.id, zone.id, k, observedOn));
  const rainDiscount = -Math.min(0.35, ep.rain72 / 90);
  const extent = Math.min(0.32, 0.12 + zone.area_km2 / 25);
  const naturalCause = ep.rain72 > 20;
  const confidence = Math.max(0.42, Math.min(0.96, 0.55 + Math.abs(primary.z_score) / 25 + rainDiscount));
  const priority = Math.round(
    Math.min(99, severityFloor[ep.severity] + Math.abs(primary.z_score) * 1.6 + (wb.drinking_water ? 8 : 0) + (wb.urban ? 4 : 0) + rainDiscount * 40 + r() * 4),
  );
  const isKhadakwasla = wb.id === "wb_khadakwasla";
  const label = primary.key.split("_")[1];
  return {
    alert_id: `alr_${observedOn.slice(0, 4)}_${observedOn.slice(5, 7)}${observedOn.slice(8, 10)}_${wb.id.replace("wb_", "")}_z${ep.zone}`,
    water_body: { id: wb.id, name: wb.name, district: wb.district },
    zone: { id: zone.id, name: zone.name, centroid: zone.centroid },
    observed_on: observedOn,
    affected_area_km2: isKhadakwasla ? 2.47 : +Math.max(0.05, zone.area_km2 * (0.35 + r() * 0.5)).toFixed(2),
    primary_indicator: ep.indicator,
    severity: ep.severity,
    confidence: isKhadakwasla ? 0.89 : +confidence.toFixed(2),
    priority_score: isKhadakwasla ? 87 : priority,
    status: ep.status,
    indicators: [
      isKhadakwasla
        ? { key: "ndti_turbidity", value: 0.312, baseline_mean: 0.128, baseline_std: 0.021, z_score: 8.76, deviation_pct: 143.8 }
        : primary,
      ...others,
    ],
    explanation: {
      summary: isKhadakwasla
        ? "Flagged because the turbidity indicator is 2.4x its seasonal baseline across 2.47 km2 of the eastern zone, with a correlated rise in suspended sediment."
        : `Flagged because the ${label} indicator is ${(primary.value / primary.baseline_mean).toFixed(1)}x its seasonal baseline across the ${zone.name.toLowerCase()} of ${wb.name}${naturalCause ? ", although rainfall in the preceding 72h is a plausible natural cause" : ""}.`,
      contributions: isKhadakwasla
        ? [
            { factor: "Turbidity deviation from baseline", value: 0.41 },
            { factor: "Spatial extent of affected pixels", value: 0.24 },
            { factor: "Suspended sediment correlation", value: 0.19 },
            { factor: "Rainfall in preceding 72h", value: -0.12 },
          ]
        : [
            { factor: `${label[0].toUpperCase()}${label.slice(1)} deviation from baseline`, value: +(0.28 + Math.abs(primary.z_score) / 40).toFixed(2) },
            { factor: "Spatial extent of affected pixels", value: +extent.toFixed(2) },
            { factor: "Persistence across consecutive passes", value: +(0.08 + r() * 0.14).toFixed(2) },
            { factor: wb.drinking_water ? "Drinking water source weighting" : "Downstream population weighting", value: +(0.05 + r() * 0.1).toFixed(2) },
            { factor: "Rainfall in preceding 72h", value: +rainDiscount.toFixed(2) },
            { factor: "Cloud contamination of pixels", value: +(-observed.cloud_cover_pct / 400).toFixed(2) },
          ],
    },
    context: {
      rainfall_72h_mm: ep.rain72,
      cloud_cover_pct: isKhadakwasla ? 8.1 : observed.cloud_cover_pct,
      natural_cause_likely: naturalCause,
    },
    evidence: {
      baseline_composite_url: `/tiles/baseline/${wb.id}/${zone.id}`,
      current_observation_url: `/tiles/current/${wb.id}/${observedOn}/${zone.id}`,
      anomaly_mask_url: `/tiles/anomaly/${wb.id}/${observedOn}/${zone.id}`,
    },
    disclaimer: DISCLAIMER,
  };
}

export const ALERTS: Alert[] = EPISODES.map(buildAlert).filter((a): a is Alert => a !== null);
export const alertZoneGeometry = (a: Alert): Geometry | undefined =>
  RAW.find((b) => b.id === a.water_body.id)?.zones.find((z) => z.id === a.zone.id)?.geometry;

// ---------- Water body summaries ----------
function statusFor(wbId: string): { status: BodyStatus; open: number; max: number | null } {
  const open = ALERTS.filter((a) => a.water_body.id === wbId && (a.status === "open" || a.status === "investigating"));
  const max = open.length ? Math.max(...open.map((a) => a.priority_score)) : null;
  const obs = observationsFor(wbId);
  if (!obs.some((o) => o.usable && o.observed_on >= isoDay(subDays(today, 30)))) return { status: "no_data", open: open.length, max };
  if (max !== null && max >= 60) return { status: "alert", open: open.length, max };
  if (open.length) return { status: "watch", open: open.length, max };
  return { status: "normal", open: 0, max };
}

export function summaryFor(b: RawBody): WaterBodySummary {
  const s = statusFor(b.id);
  const latest = [...observationsFor(b.id)].reverse().find((o) => o.usable) ?? null;
  return {
    id: b.id,
    name: b.name,
    district: b.district,
    kind: b.kind,
    tier: b.tier,
    area_km2: b.area_km2,
    centroid: b.centroid,
    status: s.status,
    open_alerts: s.open,
    max_priority: s.max,
    latest_observation: latest
      ? { scene_id: latest.scene_id, observed_on: latest.observed_on, cloud_cover_pct: latest.cloud_cover_pct, usable: latest.usable }
      : null,
  };
}

export const BODIES = RAW;
export function detailFor(id: string): WaterBodyDetail | undefined {
  const b = RAW.find((x) => x.id === id);
  if (!b) return undefined;
  return { ...summaryFor(b), drinking_water: b.drinking_water, urban: b.urban, bbox: b.bbox, boundary: b.geometry, zones: b.zones };
}
