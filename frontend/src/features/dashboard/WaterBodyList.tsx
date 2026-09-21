import { useMemo, useState } from "react";
import { Search } from "lucide-react";
import { useWaterBodies } from "@/api/hooks";
import type { WaterBodyListItem } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { EmptyState, ErrorState, PanelSkeleton } from "@/components/States";
import { fmtDateShort, fmtKm2, severityBg, statusLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

const STATUS_DOT: Record<WaterBodyListItem["status"], string> = {
  alert: "bg-red-500",
  watch: "bg-amber-500",
  normal: "bg-emerald-500",
  baseline_building: "bg-slate-400",
  no_data: "bg-slate-300",
};

export function WaterBodyList() {
  const [q, setQ] = useState("");
  const { data, isLoading, error, refetch } = useWaterBodies();
  const selected = useUi((s) => s.waterBodyId);
  const select = useUi((s) => s.selectWaterBody);

  const items = useMemo(() => {
    const rows = data?.items ?? [];
    const needle = q.trim().toLowerCase();
    const filtered = needle
      ? rows.filter((r) => `${r.name} ${r.district}`.toLowerCase().includes(needle))
      : rows;
    // Bodies with open alerts first, then by tier and name.
    return [...filtered].sort(
      (a, b) =>
        Number(b.open_alerts > 0) - Number(a.open_alerts > 0) ||
        a.tier - b.tier ||
        a.name.localeCompare(b.name),
    );
  }, [data, q]);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b p-2">
        <label className="flex items-center gap-2 rounded-md border bg-card px-2 py-1.5 text-sm">
          <Search className="h-3.5 w-3.5 text-muted-foreground" />
          <input
            className="w-full bg-transparent outline-none placeholder:text-muted-foreground"
            placeholder="Search water bodies"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </label>
      </div>
      <div className="flex-1 overflow-y-auto">
        {isLoading && <PanelSkeleton rows={8} />}
        {error && <ErrorState error={error} onRetry={() => void refetch()} />}
        {data && items.length === 0 && (
          <EmptyState title="No water bodies" hint="Seed the registry (make seed) or clear the search." />
        )}
        <ul>
          {items.map((wb) => (
            <li key={wb.id}>
              <button
                onClick={() => select(wb.id)}
                className={cn(
                  "flex w-full items-start gap-2 border-b px-3 py-2 text-left hover:bg-accent/60",
                  selected === wb.id && "bg-accent",
                )}
              >
                <span className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", STATUS_DOT[wb.status])} />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{wb.name}</span>
                    <span className="shrink-0 text-[10px] text-muted-foreground">T{wb.tier}</span>
                  </span>
                  <span className="flex items-center gap-2 text-[11px] text-muted-foreground">
                    <span>{wb.district}</span>
                    <span>·</span>
                    <span>{fmtKm2(wb.area_km2)}</span>
                    {wb.latest_observation && (
                      <>
                        <span>·</span>
                        <span>{fmtDateShort(wb.latest_observation.observed_on)}</span>
                      </>
                    )}
                  </span>
                  <span className="mt-1 flex items-center gap-1">
                    {wb.open_alerts > 0 && wb.max_open_severity ? (
                      <Badge className={severityBg[wb.max_open_severity]}>
                        {wb.open_alerts} open · {wb.max_open_severity}
                      </Badge>
                    ) : (
                      <Badge className="bg-muted text-muted-foreground">{statusLabel[wb.status]}</Badge>
                    )}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
