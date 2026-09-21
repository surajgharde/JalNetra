import { CloudRain, FileText, MapPin } from "lucide-react";
import { api } from "@/api/client";
import { useAlert, useSetAlertStatus } from "@/api/hooks";
import type { AlertOut, AlertStatus } from "@/api/types";
import { Disclaimer } from "@/components/Disclaimer";
import { ErrorState, PanelSkeleton } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { fmtDate, fmtKm2, fmtNum, fmtPct, fmtSigned, indicatorLabel, severityBg, statusLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";
import { ContributionChart } from "./ContributionChart";
import { PriorityRing } from "./PriorityRing";
import { ValidationForm } from "./ValidationForm";

const NEXT_STATUS: Record<AlertStatus, AlertStatus[]> = {
  open: ["investigating", "dismissed"],
  investigating: ["validated", "dismissed", "open"],
  validated: ["open"],
  dismissed: ["open"],
};

function StatusActions({ alert }: { alert: AlertOut }) {
  const mutate = useSetAlertStatus(alert.alert_id);
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge className="bg-muted text-foreground">{statusLabel[alert.status]}</Badge>
      {NEXT_STATUS[alert.status].map((s) => (
        <Button
          key={s}
          size="sm"
          variant={s === "dismissed" ? "outline" : "secondary"}
          disabled={mutate.isPending}
          onClick={() => {
            const note = window.prompt(`Mark as ${statusLabel[s]} — note (optional)`) ?? undefined;
            mutate.mutate({ status: s, note: note || null, by: null });
          }}
        >
          Mark {statusLabel[s].toLowerCase()}
        </Button>
      ))}
      {mutate.error && <span className="text-xs text-destructive">{mutate.error.message}</span>}
    </div>
  );
}

function Body({ alert }: { alert: AlertOut }) {
  const selectWaterBody = useUi((s) => s.selectWaterBody);
  const selectDate = useUi((s) => s.selectDate);
  const selectZone = useUi((s) => s.selectZone);
  const openAlert = useUi((s) => s.openAlert);
  const ctx = alert.context;

  return (
    <div className="space-y-4 p-5">
      <div className="flex items-start gap-4">
        <PriorityRing score={alert.priority_score} severity={alert.severity} confidence={alert.confidence} />
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge className={severityBg[alert.severity]}>{alert.severity}</Badge>
            {ctx.natural_cause_likely && (
              <Badge className="bg-sky-100 text-sky-800">
                <CloudRain className="mr-1 h-3 w-3" /> natural cause likely
              </Badge>
            )}
            <Badge className="bg-muted text-muted-foreground">{indicatorLabel(alert.primary_indicator)}</Badge>
          </div>
          <div className="text-sm">
            Observed <b>{fmtDate(alert.observed_on)}</b>
            {alert.n_observations > 1 && (
              <span className="text-muted-foreground">
                {" "}· first {fmtDate(alert.first_observed_on)} · {alert.n_observations} observations · peak{" "}
                {Math.round(alert.peak_priority_score)} ({alert.peak_severity})
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            Affected area {fmtKm2(alert.affected_area_km2)} · rainfall 72 h {fmtNum(ctx.rainfall_72h_mm, 1)} mm
            {ctx.rainfall_percentile !== null && ctx.rainfall_percentile !== undefined && ` (p${Math.round(ctx.rainfall_percentile * 100)})`}
            {" "}· cloud {fmtNum(ctx.cloud_cover_pct, 1)}% · model {alert.model_version}
          </div>
          <StatusActions alert={alert} />
        </div>
      </div>

      <section>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Why this was flagged</h3>
        <p className="text-sm leading-relaxed">{alert.explanation.summary}</p>
        {ctx.gate_reason && <p className="mt-1 text-xs text-muted-foreground">Rainfall gate: {ctx.gate_reason}</p>}
      </section>

      <section>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Contributing factors</h3>
        <ContributionChart contributions={alert.explanation.contributions} />
      </section>

      <Tabs defaultValue="indicators">
        <TabsList>
          <TabsTrigger value="indicators">Indicators</TabsTrigger>
          <TabsTrigger value="timeline">Timeline</TabsTrigger>
          <TabsTrigger value="evidence">Evidence</TabsTrigger>
          <TabsTrigger value="validate">Field validation</TabsTrigger>
        </TabsList>
        <TabsContent value="indicators" className="pt-2">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
                <th className="text-left font-medium">Indicator</th>
                <th className="text-right font-medium">Value</th>
                <th className="text-right font-medium">Baseline</th>
                <th className="text-right font-medium">z</th>
                <th className="text-right font-medium">Dev.</th>
              </tr>
            </thead>
            <tbody>
              {alert.indicators.map((r) => (
                <tr key={r.key} className={cn("border-t", r.key === alert.primary_indicator && "font-semibold")}>
                  <td className="py-1">{indicatorLabel(r.key)}</td>
                  <td className="py-1 text-right font-mono">{fmtNum(r.value)}</td>
                  <td className="py-1 text-right font-mono text-muted-foreground">
                    {fmtNum(r.baseline_mean)} ±{fmtNum(r.baseline_std)}
                  </td>
                  <td className="py-1 text-right font-mono">{fmtSigned(r.z_score)}</td>
                  <td className="py-1 text-right font-mono">{fmtPct(r.deviation_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TabsContent>
        <TabsContent value="timeline" className="pt-2">
          <ul className="space-y-1 text-xs">
            {(alert.timeline ?? []).map((t) => (
              <li key={t.scene_id} className="flex items-center gap-2 rounded border px-2 py-1">
                <span className="w-24 font-medium">{fmtDate(t.observed_at)}</span>
                {t.alertable ? (
                  <Badge className={severityBg[t.severity ?? "low"]}>{t.severity}</Badge>
                ) : (
                  <Badge className="bg-muted text-muted-foreground">below threshold</Badge>
                )}
                <span className="font-mono text-muted-foreground">
                  priority {t.priority_score === null || t.priority_score === undefined ? "—" : Math.round(t.priority_score)} · conf{" "}
                  {t.confidence === null || t.confidence === undefined ? "—" : t.confidence.toFixed(2)}
                </span>
                <button
                  className="ml-auto text-primary hover:underline"
                  onClick={() => {
                    selectWaterBody(alert.water_body.id);
                    selectDate(t.observed_at.slice(0, 10));
                    selectZone(alert.zone.id);
                    openAlert(null);
                  }}
                >
                  view scene
                </button>
              </li>
            ))}
          </ul>
        </TabsContent>
        <TabsContent value="evidence" className="space-y-2 pt-2 text-xs">
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline" size="sm">
              <a href={api.alerts.briefUrl(alert.alert_id)} target="_blank" rel="noreferrer">
                <FileText className="h-3.5 w-3.5" /> Investigation brief (PDF)
              </a>
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                selectWaterBody(alert.water_body.id);
                selectDate(alert.observed_on);
                selectZone(alert.zone.id);
                openAlert(null);
              }}
            >
              <MapPin className="h-3.5 w-3.5" /> Show on map
            </Button>
          </div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-muted-foreground">
            <dt>Current scene</dt>
            <dd className="font-mono">{alert.evidence.current_scene_id ?? "—"}</dd>
            <dt>Reference scene</dt>
            <dd className="font-mono">
              {alert.evidence.reference_scene_id ?? "—"}
              {alert.evidence.reference_observed_on && ` (${fmtDate(alert.evidence.reference_observed_on)})`}
            </dd>
            <dt>Current tiles</dt>
            <dd className="truncate font-mono">{alert.evidence.current_observation_url ?? "—"}</dd>
            <dt>Reference tiles</dt>
            <dd className="truncate font-mono">{alert.evidence.baseline_composite_url ?? "—"}</dd>
            <dt>Flagged area</dt>
            <dd className="truncate font-mono">{alert.evidence.anomaly_mask_url ?? "—"}</dd>
          </dl>
        </TabsContent>
        <TabsContent value="validate" className="pt-2">
          <ValidationForm alertId={alert.alert_id} />
        </TabsContent>
      </Tabs>

      <Disclaimer text={alert.disclaimer} />
    </div>
  );
}

/** Global slide-over; opens whenever `ui.alertId` is set (map click, queue row, deep link). */
export function AlertSheet() {
  const alertId = useUi((s) => s.alertId);
  const openAlert = useUi((s) => s.openAlert);
  const { data, isLoading, error, refetch } = useAlert(alertId);
  return (
    <Sheet
      open={!!alertId}
      onOpenChange={(o) => !o && openAlert(null)}
      title={data ? `${data.water_body.name} · ${data.zone.name}` : "Alert"}
      description={data ? `${data.water_body.district} · ${data.alert_id}` : alertId ?? ""}
    >
      {isLoading && <PanelSkeleton rows={8} />}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}
      {data && <Body alert={data} />}
    </Sheet>
  );
}
