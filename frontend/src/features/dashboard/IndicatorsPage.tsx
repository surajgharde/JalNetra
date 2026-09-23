import { Link } from "react-router-dom";
import { LineChart } from "lucide-react";
import { useIndicators, useWaterBody } from "@/api/hooks";
import type { ZoneIndicator } from "@/api/types";
import { Disclaimer } from "@/components/Disclaimer";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState, ErrorState, PanelSkeleton } from "@/components/States";
import { fmtDate, fmtNum, fmtPct, fmtSigned } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

function zClass(z: number | null | undefined, status: string) {
  if (status !== "usable") return "text-muted-foreground";
  if (z === null || z === undefined) return "";
  if (Math.abs(z) > 5) return "font-semibold text-red-700";
  if (Math.abs(z) > 3) return "font-semibold text-amber-700";
  return "";
}

/**
 * One indicator. `scored` is false while the whole zone is still building a
 * baseline: the z and deviation columns are dropped rather than filled with
 * repeated "building" text that says the same thing five times.
 */
function Row({
  r,
  onPick,
  active,
  scored,
}: {
  r: ZoneIndicator;
  onPick: () => void;
  active: boolean;
  scored: boolean;
}) {
  return (
    <tr
      className={cn("cursor-pointer border-b last:border-0 hover:bg-accent/50", active && "bg-accent")}
      onClick={onPick}
      title={r.scientific_basis}
    >
      <td className="py-1.5 pr-2 text-xs font-medium leading-tight">{r.display_name}</td>
      <td className="tabular py-1.5 pr-3 text-right font-mono text-xs">{fmtNum(r.value)}</td>
      <td className="tabular py-1.5 pr-3 text-right font-mono text-xs text-muted-foreground">
        {fmtNum(r.baseline_mean)}
        {r.baseline_std !== null && r.baseline_std !== undefined && (
          <span className="opacity-70"> ±{fmtNum(r.baseline_std)}</span>
        )}
      </td>
      {scored && (
        <>
          <td className={cn("tabular py-1.5 pr-3 text-right font-mono text-xs", zClass(r.z_score, r.baseline_status))}>
            {fmtSigned(r.z_score)}
          </td>
          <td className={cn("tabular py-1.5 text-right font-mono text-xs", zClass(r.z_score, r.baseline_status))}>
            {fmtPct(r.deviation_pct)}
          </td>
        </>
      )}
    </tr>
  );
}

/** Every zone's indicators for the selected scene, one card per zone. */
export function IndicatorsPage() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const date = useUi((s) => s.date);
  const zoneId = useUi((s) => s.zoneId);
  const selectZone = useUi((s) => s.selectZone);
  const indicator = useUi((s) => s.indicator);
  const selectIndicator = useUi((s) => s.selectIndicator);
  const body = useWaterBody(waterBodyId);
  const { data, isLoading, error, refetch } = useIndicators(waterBodyId, date ?? undefined);

  if (!waterBodyId)
    return <EmptyState title="Select a water body" hint="Pick one from the bar above to see its indicators." />;

  const zones = zoneId ? (data?.zones ?? []).filter((z) => z.zone_id === zoneId) : (data?.zones ?? []);

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Indicators"
        subtitle={
          data?.observed_on
            ? `${body.data?.name ?? waterBodyId} · scene ${fmtDate(data.observed_on)}${
                data.observed_on !== data.requested_date ? ` (latest before ${fmtDate(data.requested_date)})` : ""
              }`
            : (body.data?.name ?? waterBodyId)
        }
        actions={
          <>
            <select
              className="rounded border bg-card px-1.5 py-1 text-xs"
              value={zoneId ?? ""}
              onChange={(e) => selectZone(e.target.value || null)}
              aria-label="Zone"
            >
              <option value="">All zones</option>
              {(data?.zones ?? []).map((z) => (
                <option key={z.zone_id} value={z.zone_id}>
                  {z.zone_name}
                </option>
              ))}
            </select>
            <Link
              to="/trends"
              className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
              title="Plot the selected indicator over time"
            >
              <LineChart className="h-3.5 w-3.5" /> Trends
            </Link>
          </>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {isLoading && <PanelSkeleton rows={6} />}
        {error && <ErrorState error={error} onRetry={() => void refetch()} />}
        {data && !data.scene_id && (
          <EmptyState title="No indicators yet" hint="The pipeline has not processed a scene for this body." />
        )}
        <div className="grid items-start gap-3 md:grid-cols-2 2xl:grid-cols-3">
          {zones.map((z) => {
            const scored = z.indicators.some((r) => r.baseline_status === "usable");
            const empty = z.indicators.every((r) => r.value === null);
            return (
              <section key={z.zone_id} className="rounded-lg border bg-card p-3">
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <h2 className="truncate text-sm font-semibold">{z.zone_name}</h2>
                  <button
                    className="shrink-0 text-[11px] text-primary hover:underline"
                    onClick={() => selectZone(zoneId === z.zone_id ? null : z.zone_id)}
                  >
                    {zoneId === z.zone_id ? "Clear filter" : "Focus"}
                  </button>
                </div>
                {empty ? (
                  <p className="text-xs text-muted-foreground">
                    No usable pixels in this zone for this scene
                    {(z.rejected ?? []).length > 0 && ` (${String(z.rejected![0].reason ?? "rejected")})`}.
                  </p>
                ) : (
                  <table className="w-full">
                    <thead>
                      <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        <th className="pb-1 text-left font-medium">Indicator</th>
                        <th className="pb-1 pr-3 text-right font-medium">Now</th>
                        <th className="pb-1 pr-3 text-right font-medium">Baseline</th>
                        {scored && <th className="pb-1 pr-3 text-right font-medium">z</th>}
                        {scored && <th className="pb-1 text-right font-medium">Dev.</th>}
                      </tr>
                    </thead>
                    <tbody>
                      {z.indicators.map((r) => (
                        <Row
                          key={r.key}
                          r={r}
                          scored={scored}
                          active={indicator === r.key}
                          onPick={() => {
                            selectIndicator(r.key);
                            selectZone(z.zone_id);
                          }}
                        />
                      ))}
                    </tbody>
                  </table>
                )}
                {!scored && !empty && (
                  <p className="pt-1.5 text-[11px] leading-tight text-muted-foreground">
                    Baseline still building — no z-score or deviation for this zone yet.
                  </p>
                )}
              </section>
            );
          })}
        </div>
      </div>

      {data?.disclaimer && <Disclaimer text={data.disclaimer} className="m-2 mt-0" />}
    </div>
  );
}
