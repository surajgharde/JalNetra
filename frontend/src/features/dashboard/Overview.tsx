import { useMemo } from "react";
import { useIndicators, useObservations, useWaterBody } from "@/api/hooks";
import { EmptyState } from "@/components/States";
import { StatTile, type Tone } from "@/components/StatTile";
import { Skeleton } from "@/components/ui/skeleton";
import { fmtDate, fmtKm2, fmtSigned, indicatorShortLabel, statusLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";
import { MapView } from "./MapView";

/**
 * The landing screen: where the anomaly is (map) and the four numbers that say
 * whether it needs attention. Everything that needs a table, a time axis or a
 * queue lives one click away — this screen stays readable at a glance.
 */
export function Overview() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const date = useUi((s) => s.date);
  const body = useWaterBody(waterBodyId);
  const observations = useObservations(waterBodyId);
  const indicators = useIndicators(waterBodyId, date ?? undefined);

  // The strongest signed z-score on this scene: the headline finding, if any.
  const peak = useMemo(() => {
    let best: { z: number; label: string; zone: string } | null = null;
    for (const zone of indicators.data?.zones ?? []) {
      for (const r of zone.indicators) {
        if (r.baseline_status !== "usable" || r.z_score === null || r.z_score === undefined) continue;
        if (!best || Math.abs(r.z_score) > Math.abs(best.z))
          best = { z: r.z_score, label: indicatorShortLabel(r.key), zone: zone.zone_name };
      }
    }
    return best;
  }, [indicators.data]);

  const scene = indicators.data?.observed_on ?? null;
  const observation = useMemo(
    () => (observations.data?.items ?? []).find((o) => o.observed_on === scene),
    [observations.data, scene],
  );

  if (!waterBodyId)
    return (
      <EmptyState
        title="Select a water body"
        hint="Pick one from the bar above to see its current state."
      />
    );

  const wb = body.data;
  const loading = body.isLoading || indicators.isLoading;
  const peakTone: Tone = !peak ? "neutral" : Math.abs(peak.z) > 5 ? "critical" : Math.abs(peak.z) > 3 ? "warning" : "good";
  const clearPct = observation?.valid_pixel_pct ?? null;

  return (
    <div className="flex h-full flex-col">
      {/* Identity strip — one line, so the map keeps the space. */}
      <div className="flex items-center gap-2 border-b bg-card px-3 py-1.5 text-xs">
        <span className="truncate font-semibold">{wb?.name ?? waterBodyId}</span>
        <span className="truncate text-muted-foreground">
          {wb ? `${wb.district} · ${fmtKm2(wb.area_km2)} · tier ${wb.tier}` : ""}
        </span>
        <span className="ml-auto shrink-0 text-muted-foreground">
          {scene ? `Scene ${fmtDate(scene)}` : "No scene yet"}
        </span>
        {wb && (
          <span
            className={cn(
              "flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 font-medium",
              wb.status === "alert" && "border-red-200 bg-red-50 text-red-800",
              wb.status === "watch" && "border-amber-200 bg-amber-50 text-amber-800",
              wb.status === "normal" && "border-emerald-200 bg-emerald-50 text-emerald-800",
            )}
          >
            {statusLabel[wb.status] ?? wb.status}
          </span>
        )}
      </div>

      {/* The four headline numbers. Each one is the door to the screen behind it. */}
      <div className="grid shrink-0 grid-cols-2 gap-2 p-2 lg:grid-cols-4">
        {loading
          ? [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-[4.75rem]" />)
          : [
              <StatTile
                key="alerts"
                label="Open alerts"
                value={String(wb?.open_alerts ?? 0)}
                sub={
                  wb?.open_alerts
                    ? `Highest severity ${wb.max_open_severity ?? "—"}`
                    : "Nothing awaiting investigation"
                }
                tone={wb?.open_alerts ? (wb.max_open_severity === "high" ? "critical" : "warning") : "good"}
                to="/alerts"
                cta="Queue"
              />,
              <StatTile
                key="peak"
                label="Strongest anomaly"
                value={peak ? `${fmtSigned(peak.z)} z` : indicators.data?.scene_id ? "Building" : "—"}
                sub={peak ? `${peak.label} · ${peak.zone}` : "No baseline on this scene yet"}
                tone={peakTone}
                to="/indicators"
                cta="Indicators"
              />,
              <StatTile
                key="extent"
                label="Water extent"
                value={fmtKm2(observation?.water_extent_km2)}
                sub={wb ? `Registered outline ${fmtKm2(wb.area_km2)}` : undefined}
                tone="neutral"
                to="/trends"
                cta="Trends"
              />,
              <StatTile
                key="scene"
                label="Scene quality"
                value={clearPct === null ? "—" : `${clearPct.toFixed(0)}% clear`}
                sub={
                  observation
                    ? `${observation.cloud_pct.toFixed(0)}% cloud · ${observation.stage}`
                    : "No usable observation"
                }
                tone={clearPct === null ? "neutral" : clearPct < 40 ? "warning" : "good"}
                to="/trends"
                cta="History"
              />,
            ]}
      </div>

      <div className="min-h-0 flex-1 border-t">
        <MapView />
      </div>
    </div>
  );
}
