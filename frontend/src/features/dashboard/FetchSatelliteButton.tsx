import { format, subDays } from "date-fns";
import { Satellite } from "lucide-react";
import { useStartIngest } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { useToasts } from "@/store/toast";
import { useUi } from "@/store/ui";

const DEFAULT_WINDOW_DAYS = 30;

/**
 * One-click "fetch satellite data" for a lake that has no processed scenes
 * yet: a default 30-day window, no date pickers. Wired into `ui.jobId`, the
 * same slot the top bar's pipeline runner polls and reports progress on, so
 * starting it here surfaces there automatically -- including the invalidation
 * that refreshes observations/indicators once the run finishes.
 */
export function FetchSatelliteButton({
  waterBodyId,
  lakeName,
  className,
}: {
  waterBodyId: string;
  lakeName: string;
  className?: string;
}) {
  const start = useStartIngest();
  const watchJob = useUi((s) => s.watchJob);
  const push = useToasts((s) => s.push);

  function fetchData() {
    const to = format(new Date(), "yyyy-MM-dd");
    const from = format(subDays(new Date(), DEFAULT_WINDOW_DAYS), "yyyy-MM-dd");
    start.mutate(
      { water_body_id: waterBodyId, date_from: from, date_to: to, requested_by: "fetch-satellite-button" },
      {
        onSuccess: (j) => {
          watchJob(j.job_id);
          push(`Sentinel-2 ingestion started for ${lakeName}. Watch progress in the top bar.`, "success");
        },
        onError: (err) => push(`Could not start ingestion for ${lakeName}: ${err.message}`, "error"),
      },
    );
  }

  return (
    <Button
      size="sm"
      variant="secondary"
      disabled={start.isPending}
      onClick={fetchData}
      className={className}
      title={`Run the Sentinel-2 pipeline for ${lakeName} (last ${DEFAULT_WINDOW_DAYS} days)`}
    >
      <Satellite className="h-3.5 w-3.5" />
      {start.isPending ? "Starting…" : "Fetch satellite data"}
    </Button>
  );
}
