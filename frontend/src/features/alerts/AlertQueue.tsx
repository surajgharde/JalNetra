import { useState } from "react";
import { CloudRain } from "lucide-react";
import { useAlerts } from "@/api/hooks";
import type { AlertFilters, Severity } from "@/api/types";
import { Disclaimer } from "@/components/Disclaimer";
import { EmptyState, ErrorState, PanelSkeleton } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { fmtDate, fmtKm2, indicatorLabel, severityBg, severityColor, statusLabel } from "@/lib/format";
import { useUi } from "@/store/ui";

/** Priority queue: every alert, priority descending, with server-side filters. */
export function AlertQueue() {
  const [filters, setFilters] = useState<AlertFilters>({ status: "active" });
  const { data, isLoading, error, refetch } = useAlerts({ ...filters, limit: 200 });
  const openAlert = useUi((s) => s.openAlert);

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2 text-xs">
        <label className="flex items-center gap-1">
          Status
          <select
            className="rounded border bg-card px-1 py-0.5"
            value={filters.status ?? "active"}
            onChange={(e) => setFilters({ ...filters, status: e.target.value as AlertFilters["status"] })}
          >
            <option value="active">Open + investigating</option>
            <option value="open">Open</option>
            <option value="investigating">Investigating</option>
            <option value="validated">Validated</option>
            <option value="dismissed">Dismissed</option>
            <option value="all">All</option>
          </select>
        </label>
        <label className="flex items-center gap-1">
          Min severity
          <select
            className="rounded border bg-card px-1 py-0.5"
            value={filters.severity ?? ""}
            onChange={(e) => setFilters({ ...filters, severity: (e.target.value || undefined) as Severity | undefined })}
          >
            <option value="">any</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
          </select>
        </label>
        <label className="flex items-center gap-1">
          Min priority
          <input
            type="number"
            min={0}
            max={100}
            className="w-16 rounded border bg-card px-1 py-0.5 font-mono"
            value={filters.min_priority ?? ""}
            onChange={(e) =>
              setFilters({ ...filters, min_priority: e.target.value === "" ? undefined : Number(e.target.value) })
            }
          />
        </label>
        <label className="flex items-center gap-1">
          District
          <input
            className="w-28 rounded border bg-card px-1 py-0.5"
            placeholder="Pune"
            value={filters.district ?? ""}
            onChange={(e) => setFilters({ ...filters, district: e.target.value || undefined })}
          />
        </label>
        {data && <span className="ml-auto text-muted-foreground">{data.total} alerts</span>}
      </div>
      <div className="flex-1 overflow-auto">
        {isLoading && <PanelSkeleton rows={8} />}
        {error && <ErrorState error={error} onRetry={() => void refetch()} />}
        {data && data.items.length === 0 && (
          <EmptyState title="No alerts match" hint="Widen the filters, or run the pipeline on a water body." />
        )}
        {data && data.items.length > 0 && (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-card">
              <tr className="border-b text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                <th className="px-3 py-2 font-medium">Priority</th>
                <th className="py-2 font-medium">Water body · zone</th>
                <th className="py-2 font-medium">Indicator</th>
                <th className="py-2 font-medium">Observed</th>
                <th className="py-2 font-medium">Area</th>
                <th className="py-2 font-medium">Conf.</th>
                <th className="py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((a) => (
                <tr
                  key={a.alert_id}
                  className="cursor-pointer border-b hover:bg-accent/60"
                  onClick={() => openAlert(a.alert_id)}
                >
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span
                        className="inline-flex h-8 w-8 items-center justify-center rounded-full font-mono text-[11px] font-bold text-white"
                        style={{ background: severityColor[a.severity] }}
                      >
                        {Math.round(a.priority_score)}
                      </span>
                      <Badge className={severityBg[a.severity]}>{a.severity}</Badge>
                      {a.natural_cause_likely && <CloudRain className="h-3.5 w-3.5 text-sky-600" aria-label="natural cause likely" />}
                    </div>
                  </td>
                  <td className="py-2">
                    <div className="font-medium">{a.water_body.name}</div>
                    <div className="text-muted-foreground">
                      {a.zone.name} · {a.water_body.district}
                    </div>
                  </td>
                  <td className="py-2">{indicatorLabel(a.primary_indicator)}</td>
                  <td className="py-2">
                    {fmtDate(a.observed_on)}
                    {a.n_observations > 1 && <span className="text-muted-foreground"> ×{a.n_observations}</span>}
                  </td>
                  <td className="py-2 font-mono">{fmtKm2(a.affected_area_km2)}</td>
                  <td className="py-2 font-mono">{a.confidence.toFixed(2)}</td>
                  <td className="py-2">{statusLabel[a.status]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {data && <Disclaimer text={data.disclaimer} className="m-2" />}
    </div>
  );
}
