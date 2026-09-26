import { useState } from "react";
import { format, subDays } from "date-fns";
import { CheckCircle2, FileDown, Loader2, Play, Table2, XCircle } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useJob, useStartIngest, useWaterBody } from "@/api/hooks";
import { Button } from "@/components/ui/button";
import { STAGE_LABELS } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

/**
 * "Run pipeline" for the selected body: POST /jobs/ingest, then poll
 * GET /jobs/{id} every 2 s and show the stage in flight. When the job
 * finishes, every panel is invalidated so the new scene appears live.
 */
export function PipelineRunner() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const jobId = useUi((s) => s.jobId);
  const watchJob = useUi((s) => s.watchJob);
  const body = useWaterBody(waterBodyId);
  const start = useStartIngest();
  const job = useJob(jobId);
  const qc = useQueryClient();
  const [from, setFrom] = useState(format(subDays(new Date(), 14), "yyyy-MM-dd"));
  const [to, setTo] = useState(format(new Date(), "yyyy-MM-dd"));
  const [notified, setNotified] = useState<string | null>(null);

  const status = job.data?.status;
  if (job.data && (status === "done" || status === "failed") && notified !== job.data.job_id) {
    setNotified(job.data.job_id);
    void qc.invalidateQueries();
  }

  return (
    <div className="flex flex-nowrap items-center gap-3 text-xs">
      {/* One below the other rather than side by side with an arrow: half
       * the horizontal footprint, so the water-body strip keeps its room. */}
      <div className="flex shrink-0 flex-col gap-1">
        <label className="flex items-center gap-1.5">
          <span className="w-7 shrink-0 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            From
          </span>
          <input
            type="date"
            value={from}
            max={to}
            onChange={(e) => setFrom(e.target.value)}
            className="h-6 w-[9.5rem] rounded border bg-card px-1.5 text-[11px]"
          />
        </label>
        <label className="flex items-center gap-1.5">
          <span className="w-7 shrink-0 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            To
          </span>
          <input
            type="date"
            value={to}
            min={from}
            onChange={(e) => setTo(e.target.value)}
            className="h-6 w-[9.5rem] rounded border bg-card px-1.5 text-[11px]"
          />
        </label>
      </div>
      <Button
        size="sm"
        className="shrink-0"
        disabled={!waterBodyId || start.isPending}
        onClick={() =>
          waterBodyId &&
          start.mutate(
            { water_body_id: waterBodyId, date_from: from, date_to: to, requested_by: "dashboard" },
            { onSuccess: (j) => watchJob(j.job_id) },
          )
        }
        title={waterBodyId ? `Run the pipeline for ${body.data?.name ?? waterBodyId}` : "Select a water body first"}
      >
        <Play className="h-3.5 w-3.5" /> Run pipeline
      </Button>
      {start.error && <span className="shrink-0 text-destructive">{start.error.message}</span>}
      {job.data && <JobReadout job={job.data} onDismiss={() => watchJob(null)} />}
    </div>
  );
}

function JobReadout({
  job,
  onDismiss,
}: {
  job: NonNullable<ReturnType<typeof useJob>["data"]>;
  onDismiss: () => void;
}) {
  const running = job.status === "queued" || job.status === "running";
  return (
    <div className="flex shrink-0 items-center gap-2.5 rounded-md border bg-card px-2.5 py-1.5">
      {running && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-primary" />}
      {job.status === "done" && <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-600" />}
      {job.status === "failed" && <XCircle className="h-3.5 w-3.5 shrink-0 text-destructive" />}
      <div className="w-44 shrink-0">
        <div className="flex justify-between gap-2">
          <span className="truncate font-medium">
            {job.status === "done"
              ? `Done · ${job.scenes_usable} scene${job.scenes_usable === 1 ? "" : "s"}`
              : job.status === "failed"
                ? "Failed"
                : (STAGE_LABELS[job.current_stage ?? ""] ?? job.current_stage ?? "Queued")}
          </span>
          <span className="shrink-0 font-mono text-muted-foreground">{Math.round(job.progress_pct)}%</span>
        </div>
        <div className="mt-1 flex gap-0.5">
          {job.stages.map((s) => (
            <div key={s.stage} className="h-1.5 flex-1 overflow-hidden rounded-sm bg-muted" title={`${STAGE_LABELS[s.stage] ?? s.stage}: ${s.done}/${s.total}`}>
              <div
                className={cn("h-full", s.pct >= 100 ? "bg-emerald-500" : "bg-primary")}
                style={{ width: `${s.pct}%` }}
              />
            </div>
          ))}
        </div>
      </div>
      {job.alerts_created > 0 && (
        <span className="shrink-0 rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-semibold text-red-800">
          {job.alerts_created} alert{job.alerts_created === 1 ? "" : "s"}
        </span>
      )}
      {job.error && (
        <span className="max-w-[12rem] shrink-0 truncate text-destructive" title={job.error}>
          {job.error}
        </span>
      )}
      {job.scenes_found > 0 && (
        <span className="flex shrink-0 items-center gap-1.5 border-l pl-2.5">
          <a
            href={api.jobs.reportUrl(job.job_id, "pdf")}
            download
            className="flex h-7 items-center gap-1 rounded border px-2 hover:bg-muted"
            title={
              running
                ? "Report of what has been processed so far (re-download when the run finishes)"
                : "PDF: summary, trends, and one page per observed day with the satellite image, water mask, turbidity and chlorophyll rasters and per-zone data"
            }
          >
            <FileDown className="h-3.5 w-3.5" /> Report{running ? " (partial)" : ""}
          </a>
          <a
            href={api.jobs.reportUrl(job.job_id, "csv")}
            download
            className="flex h-7 items-center gap-1 rounded border px-2 hover:bg-muted"
            title="CSV: one row per day and zone with every indicator, z-score, priority and alert id"
          >
            <Table2 className="h-3.5 w-3.5" /> CSV
          </a>
        </span>
      )}
      {!running && (
        <button
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          onClick={onDismiss}
          aria-label="Dismiss"
        >
          ×
        </button>
      )}
    </div>
  );
}
