import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useWaterBodies } from "@/api/hooks";
import type { WaterBodyListItem } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { ErrorState } from "@/components/States";
import { Skeleton } from "@/components/ui/skeleton";
import { PipelineRunner } from "@/features/jobs/PipelineRunner";
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

/**
 * The water-body context bar across the top: search, one chip per body, and
 * the pipeline runner for whichever is selected. Bodies with open alerts sort
 * first, so the queue that matters is reachable without scrolling.
 */
export function WaterBodyBar() {
  const [q, setQ] = useState("");
  const { data, isLoading, error, refetch } = useWaterBodies();
  const selected = useUi((s) => s.waterBodyId);
  const select = useUi((s) => s.selectWaterBody);
  const stripRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLButtonElement>(null);

  const items = useMemo(() => {
    const rows = data?.items ?? [];
    const needle = q.trim().toLowerCase();
    const filtered = needle
      ? rows.filter((r) =>
          `${r.name} ${r.district}`.toLowerCase().includes(needle),
        )
      : rows;
    return [...filtered].sort(
      (a, b) =>
        Number(b.open_alerts > 0) - Number(a.open_alerts > 0) ||
        a.tier - b.tier ||
        a.name.localeCompare(b.name),
    );
  }, [data, q]);

  // The strip scrolls; keep the selected body visible when it changes.
  useEffect(() => {
    const el = activeRef.current;
    const strip = stripRef.current;
    if (!el || !strip) return;
    // Rect maths, not offsetLeft: the strip is not the element's offsetParent.
    const er = el.getBoundingClientRect();
    const sr = strip.getBoundingClientRect();
    const left =
      strip.scrollLeft + (er.left - sr.left) - sr.width / 2 + er.width / 2;
    strip.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
  }, [selected, items.length]);

  return (
    <div className="flex min-w-0 items-center gap-3 border-b bg-card px-3 py-2">
      <label className="flex w-48 shrink-0 items-center gap-2 rounded-md border bg-card px-2 py-1.5 text-sm">
        <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <input
          className="w-full bg-transparent outline-none placeholder:text-muted-foreground"
          placeholder="Search water bodies"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </label>

      <div className="relative min-w-0 flex-1">
        <div
          ref={stripRef}
          className="flex items-center gap-2 overflow-x-auto py-0.5"
        >
          {isLoading &&
            [0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-10 w-44 shrink-0 rounded-md" />
            ))}
          {error && <ErrorState error={error} onRetry={() => void refetch()} />}
          {data && items.length === 0 && (
            <span className="text-xs text-muted-foreground">
              No water bodies match — seed the registry or clear the search.
            </span>
          )}
          {items.map((wb) => (
            <button
              key={wb.id}
              ref={selected === wb.id ? activeRef : undefined}
              onClick={() => select(wb.id)}
              title={`${wb.name} · ${wb.district} · ${fmtKm2(wb.area_km2)} · tier ${wb.tier} · ${statusLabel[wb.status]}`}
              className={cn(
                "flex shrink-0 items-center gap-2 rounded-md border px-2.5 py-1 text-left transition-colors",
                selected === wb.id
                  ? "border-primary bg-accent"
                  : "hover:bg-accent/60",
              )}
            >
              <span
                className={cn(
                  "h-2 w-2 shrink-0 rounded-full",
                  STATUS_DOT[wb.status],
                )}
              />
              <span className="leading-tight">
                <span className="block whitespace-nowrap text-xs font-medium">
                  {wb.name}
                </span>
                <span className="block whitespace-nowrap text-[10px] text-muted-foreground">
                  {wb.district} · {fmtKm2(wb.area_km2)}
                  {wb.latest_observation &&
                    ` · ${fmtDateShort(wb.latest_observation.observed_on)}`}
                </span>
              </span>
              {wb.open_alerts > 0 && wb.max_open_severity && (
                <Badge
                  className={cn("shrink-0", severityBg[wb.max_open_severity])}
                  title={`${wb.open_alerts} open`}
                >
                  {wb.open_alerts}
                </Badge>
              )}
            </button>
          ))}
        </div>
        {/* Hints that the strip keeps going; the registry is longer than the bar. */}
        <div className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-card to-transparent" />
      </div>

      <div className="shrink-0 border-l pl-3">
        <PipelineRunner />
      </div>
    </div>
  );
}
