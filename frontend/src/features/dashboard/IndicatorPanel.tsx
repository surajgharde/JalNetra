import { useIndicators, useWaterBody } from "@/api/hooks";
import type { ZoneIndicator } from "@/api/types";
import { Disclaimer } from "@/components/Disclaimer";
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
 * repeated "building" text, which otherwise crushes the 23rem panel.
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
      <td className="py-1 pr-2 text-xs font-medium leading-tight">{r.display_name}</td>
      <td className="tabular py-1 pr-2 text-right align-top font-mono text-xs">{fmtNum(r.value)}</td>
      <td className="tabular py-1 pr-2 text-right align-top font-mono text-[11px] leading-tight text-muted-foreground">
        {fmtNum(r.baseline_mean)}
        {r.baseline_std !== null && r.baseline_std !== undefined && (
          <span className="block opacity-70">±{fmtNum(r.baseline_std)}</span>
        )}
      </td>
      {scored && (
        <>
          <td
            className={cn(
              "tabular py-1 pr-2 text-right align-top font-mono text-xs",
              zClass(r.z_score, r.baseline_status),
            )}
            title={r.baseline_status === "usable" ? undefined : `baseline ${r.baseline_status}`}
          >
            {r.baseline_status === "usable" ? fmtSigned(r.z_score) : "–"}
          </td>
          <td
            className={cn("tabular py-1 text-right align-top font-mono text-xs", zClass(r.z_score, r.baseline_status))}
          >
            {fmtPct(r.deviation_pct)}
          </td>
        </>
      )}
    </tr>
  );
}

export function IndicatorPanel() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const date = useUi((s) => s.date);
  const zoneId = useUi((s) => s.zoneId);
  const selectZone = useUi((s) => s.selectZone);
  const indicator = useUi((s) => s.indicator);
  const selectIndicator = useUi((s) => s.selectIndicator);
  const body = useWaterBody(waterBodyId);
  const { data, isLoading, error, refetch } = useIndicators(waterBodyId, date ?? undefined);

  if (!waterBodyId)
    return <EmptyState title="Select a water body" hint="Pick one from the list to see its indicators." />;
  if (isLoading) return <PanelSkeleton rows={6} />;
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />;
  if (!data || !data.scene_id)
    return <EmptyState title="No indicators yet" hint="The pipeline has not processed a scene for this body." />;

  const zones = zoneId ? data.zones.filter((z) => z.zone_id === zoneId) : data.zones;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-2 border-b px-3 py-2 text-xs">
        <div className="min-w-0 leading-tight" title={body.data?.name ?? waterBodyId}>
          <span className="font-medium">{body.data?.name ?? waterBodyId}</span>
          <span className="text-muted-foreground"> · scene {fmtDate(data.observed_on!)}</span>
          {data.observed_on !== data.requested_date && (
            <span className="text-muted-foreground"> (latest before {fmtDate(data.requested_date)})</span>
          )}
        </div>
        <select
          className="max-w-[9rem] shrink-0 rounded border bg-card px-1 py-0.5 text-xs"
          value={zoneId ?? ""}
          onChange={(e) => selectZone(e.target.value || null)}
        >
          <option value="">All zones</option>
          {data.zones.map((z) => (
            <option key={z.zone_id} value={z.zone_id}>
              {z.zone_name}
            </option>
          ))}
        </select>
      </div>
      <div className="flex-1 overflow-y-auto px-3">
        {zones.map((z) => {
          const scored = z.indicators.some((r) => r.baseline_status === "usable");
          return (
          <div key={z.zone_id} className="py-1.5">
            <button
              className={cn("mb-1 text-xs font-semibold hover:underline", zoneId === z.zone_id && "text-primary")}
              onClick={() => selectZone(zoneId === z.zone_id ? null : z.zone_id)}
            >
              {z.zone_name}
            </button>
            {z.indicators.every((r) => r.value === null) ? (
              <div className="text-xs text-muted-foreground">
                No usable pixels in this zone for this scene
                {(z.rejected ?? []).length > 0 && ` (${String(z.rejected![0].reason ?? "rejected")})`}.
              </div>
            ) : (
              <table className="w-full table-fixed">
                <colgroup>
                  <col />
                  <col className="w-[3.75rem]" />
                  <col className="w-[3.75rem]" />
                  {scored && <col className="w-[2.75rem]" />}
                  {scored && <col className="w-[3.25rem]" />}
                </colgroup>
                <thead>
                  <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    <th className="pb-1 text-left font-medium">Indicator</th>
                    <th className="pb-1 pr-2 text-right font-medium">Now</th>
                    <th className="pb-1 pr-2 text-right font-medium">Base</th>
                    {scored && <th className="pb-1 pr-2 text-right font-medium">z</th>}
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
            {!scored && z.indicators.some((r) => r.value !== null) && (
              <div className="pt-1 text-[10px] leading-tight text-muted-foreground">
                Baseline still building — no z-score or deviation for this zone yet.
              </div>
            )}
          </div>
          );
        })}
      </div>
      <Disclaimer text={data.disclaimer} className="m-2" />
    </div>
  );
}
