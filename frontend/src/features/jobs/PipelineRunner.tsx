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
    <div className="flex items-center gap-2 text-xs">
      <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} className="rounded border bg-card px-1 py-0.5" />
      <span className="text-muted-foreground">→</span>
      <input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} className="rounded border bg-card px-1 py-0.5" />
      <Button
        size="sm"
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
      {start.error && <span className="text-destructive">{start.error.message}</span>}
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
    <div className="flex items-center gap-2 rounded-md border bg-card px-2 py-1">
      {running && <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />}
      {job.status === "done" && <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />}
      {job.status === "failed" && <XCircle className="h-3.5 w-3.5 text-destructive" />}
      <div className="w-40">
        <div className="flex justify-between">
          <span className="font-medium">
            {job.status === "done"
              ? `Done · ${job.scenes_usable} scene${job.scenes_usable === 1 ? "" : "s"}`
              : job.status === "failed"
                ? "Failed"
                : (STAGE_LABELS[job.current_stage ?? ""] ?? job.current_stage ?? "Queued")}
          </span>
          <span className="font-mono text-muted-foreground">{Math.round(job.progress_pct)}%</span>
        </div>
        <div className="mt-0.5 flex gap-0.5">
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
        <span className="rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-semibold text-red-800">
          {job.alerts_created} alert{job.alerts_created === 1 ? "" : "s"}
        </span>
      )}
      {job.error && <span className="max-w-[12rem] truncate text-destructive" title={job.error}>{job.error}</span>}
      {job.scenes_found > 0 && (
        <span className="flex items-center gap-1 border-l pl-2">
          <a
            href={api.jobs.reportUrl(job.job_id, "pdf")}
            download
            className="flex items-center gap-1 rounded border px-1.5 py-0.5 hover:bg-muted"
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
            className="flex items-center gap-1 rounded border px-1.5 py-0.5 hover:bg-muted"
            title="CSV: one row per day and zone with every indicator, z-score, priority and alert id"
          >
            <Table2 className="h-3.5 w-3.5" /> CSV
          </a>
        </span>
      )}
      {!running && (
        <button className="text-muted-foreground hover:text-foreground" onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      )}
    </div>
  );
}
