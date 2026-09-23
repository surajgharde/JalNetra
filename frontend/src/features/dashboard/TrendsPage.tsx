import { Link } from "react-router-dom";
import { Table2 } from "lucide-react";
import { useWaterBody } from "@/api/hooks";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/States";
import { useUi } from "@/store/ui";
import { SeriesChart } from "./SeriesChart";
import { TimelineScrubber } from "./TimelineScrubber";

/**
 * The time dimension, full width: the indicator against its seasonal band, and
 * the scene timeline that drives every other screen's date.
 */
export function TrendsPage() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const body = useWaterBody(waterBodyId);

  if (!waterBodyId)
    return <EmptyState title="Select a water body" hint="Pick one from the bar above to see its history." />;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Trends"
        subtitle={`${body.data?.name ?? waterBodyId} · indicator against its seasonal p10–p90 band`}
        actions={
          <Link
            to="/indicators"
            className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-accent"
            title="Per-zone indicator table for the selected scene"
          >
            <Table2 className="h-3.5 w-3.5" /> Indicators
          </Link>
        }
      />
      <div className="min-h-0 flex-1">
        <SeriesChart />
      </div>
      <div className="shrink-0 border-t bg-card">
        <TimelineScrubber />
      </div>
    </div>
  );
}
