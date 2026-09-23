import { useEffect, useMemo, useRef } from "react";
import { ChevronLeft, ChevronRight, Cloud, CloudOff } from "lucide-react";
import { useObservations } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, PanelSkeleton } from "@/components/States";
import { fmtDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

/**
 * Scrub through the scenes the pipeline touched for the selected body.
 * Selecting a date drives the raster layers and the indicator panel.
 */
export function TimelineScrubber() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const date = useUi((s) => s.date);
  const selectDate = useUi((s) => s.selectDate);
  const { data, isLoading, error, refetch } = useObservations(waterBodyId);
  const stripRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLButtonElement>(null);

  // Oldest -> newest for the scrubber.
  const items = useMemo(() => [...(data?.items ?? [])].reverse(), [data]);
  const usable = useMemo(() => items.filter((o) => o.usable), [items]);
  const idx = usable.findIndex((o) => o.observed_on === date);

  // The strip scrolls once a body has years of scenes; keep the selection visible.
  useEffect(() => {
    const el = activeRef.current;
    const strip = stripRef.current;
    if (!el || !strip) return;
    // Rect maths, not offsetLeft: the strip is not the element's offsetParent.
    const er = el.getBoundingClientRect();
    const sr = strip.getBoundingClientRect();
    const left = strip.scrollLeft + (er.left - sr.left) - sr.width / 2 + er.width / 2;
    strip.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
  }, [date, items.length]);

  if (!waterBodyId) return null;
  if (isLoading) return <PanelSkeleton rows={2} />;
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />;
  if (!items.length)
    return <EmptyState title="No observations yet" hint="Run the pipeline for this water body." />;

  return (
    <div className="flex min-w-0 items-center gap-2 px-3 py-2">
      <Button
        size="icon"
        variant="outline"
        disabled={idx <= 0}
        onClick={() => selectDate(usable[idx - 1].observed_on)}
        aria-label="Previous observation"
      >
        <ChevronLeft className="h-4 w-4" />
      </Button>
      <div ref={stripRef} className="flex min-w-0 flex-1 items-end gap-[3px] overflow-x-auto py-1">
        {items.map((o) => {
          const active = o.observed_on === date;
          return (
            <button
              key={o.scene_id}
              ref={active ? activeRef : undefined}
              title={`${fmtDate(o.observed_on)} · cloud ${o.cloud_pct.toFixed(0)}% · ${o.usable ? `${(o.valid_pixel_pct ?? 0).toFixed(0)}% clear` : "unusable"} · ${o.stage}`}
              disabled={!o.usable}
              onClick={() => selectDate(o.observed_on)}
              className={cn(
                "h-6 w-2 shrink-0 rounded-sm transition-colors",
                o.usable ? "bg-sky-400 hover:bg-sky-600" : "bg-slate-200",
                active && "bg-sky-800 ring-2 ring-sky-300",
              )}
              style={{ height: `${Math.max(8, Math.round((o.valid_pixel_pct ?? 10) / 4))}px` }}
            />
          );
        })}
      </div>
      <Button
        size="icon"
        variant="outline"
        disabled={idx < 0 || idx >= usable.length - 1}
        onClick={() => selectDate(usable[idx + 1].observed_on)}
        aria-label="Next observation"
      >
        <ChevronRight className="h-4 w-4" />
      </Button>
      <div className="w-36 shrink-0 text-right text-xs">
        {idx >= 0 ? (
          <>
            <div className="font-medium">{fmtDate(usable[idx].observed_on)}</div>
            <div className="flex items-center justify-end gap-1 text-muted-foreground">
              {usable[idx].cloud_pct > 30 ? <Cloud className="h-3 w-3" /> : <CloudOff className="h-3 w-3" />}
              {(usable[idx].valid_pixel_pct ?? 0).toFixed(0)}% clear · {usable[idx].stage}
            </div>
          </>
        ) : (
          <span className="text-muted-foreground">{usable.length} usable of {items.length}</span>
        )}
      </div>
    </div>
  );
}
