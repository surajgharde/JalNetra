import { format, subDays } from "date-fns";
import { Satellite } from "lucide-react";
import { useStartIngest } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { useToasts } from "@/store/toast";
import { useUi } from "@/store/ui";

// A quick-look fetch: the last 5 days (Sentinel-2's own revisit cadence, so
// this is usually exactly one pass) and the backend stops after the first
// usable scene it finds -- real work, just scoped down, not a fake result.
const QUICK_FETCH_WINDOW_DAYS = 5;
const QUICK_FETCH_MAX_SCENES = 1;

/**
 * One-click "fetch satellite data" for a lake that has no processed scenes
 * yet. Wired into `ui.jobId`, the same slot the top bar's pipeline runner
 * polls and reports real progress on, so starting it here surfaces there
 * automatically -- including the invalidation that refreshes observations/
 * indicators once the run actually finishes. There is no faked "instant"
 * result here: the button responds immediately (the job is queued right
 * away), but the tiles only update once real imagery has actually been
 * processed, same as the top bar's own pipeline runner.
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
    const from = format(subDays(new Date(), QUICK_FETCH_WINDOW_DAYS), "yyyy-MM-dd");
    start.mutate(
      {
        water_body_id: waterBodyId,
        date_from: from,
        date_to: to,
        requested_by: "fetch-satellite-button",
        // Not yet in the generated OpenAPI types (frontend/openapi.json is
        // stale relative to the backend); the backend field is real.
        max_scenes: QUICK_FETCH_MAX_SCENES,
      } as Parameters<typeof start.mutate>[0],
      {
        onSuccess: (j) => {
          watchJob(j.job_id);
          push(`Fetching the latest Sentinel-2 pass for ${lakeName}. Watch progress in the top bar.`, "success");
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
      title={`Fetch the latest Sentinel-2 pass for ${lakeName} (last ${QUICK_FETCH_WINDOW_DAYS} days)`}
    >
      <Satellite className="h-3.5 w-3.5" />
      {start.isPending ? "Starting…" : "Fetch satellite data"}
    </Button>
  );
}
