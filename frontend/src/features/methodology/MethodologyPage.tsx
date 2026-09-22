/**
 * Methodology: how the platform gets from pixels to a prioritised alert.
 * Rendered from GET /api/v1/methodology, which is generated from the code that
 * does the work (indicator registry, priority weights, settings) — so what a
 * reviewer reads here is what actually runs.
 */
import { useQuery } from "@tanstack/react-query";
import { BookOpen, ChevronRight } from "lucide-react";
import { api } from "@/api/client";
import type { Methodology, MethodologyStep } from "@/api/types";
import { Disclaimer } from "@/components/Disclaimer";

function StepCard({ step }: { step: MethodologyStep }) {
  return (
    <section className="rounded-lg border bg-card p-4">
      <h3 className="text-sm font-semibold">{step.title}</h3>
      <p className="mt-1 text-sm text-muted-foreground">{step.summary}</p>
      <ul className="mt-2 space-y-1 text-xs">
        {step.details.map((d, i) => (
          <li key={i} className="flex gap-1.5">
            <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-primary" />
            <span>{d}</span>
          </li>
        ))}
      </ul>
      {Object.keys(step.parameters ?? {}).length > 0 && (
        <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
          {Object.entries(step.parameters ?? {}).map(([k, v]) => (
            <div key={k} className="flex gap-1">
              <dt className="font-mono">{k}:</dt>
              <dd className="font-mono">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}

function IndicatorTable({ m }: { m: Methodology }) {
  return (
    <section className="rounded-lg border bg-card p-4">
      <h3 className="text-sm font-semibold">Spectral indicators and their scientific basis</h3>
      <p className="mt-1 text-sm text-muted-foreground">
        Each indicator is computed per pixel over detected water (MNDWI over the whole zone), then aggregated per
        zone and pass. All are proxies for optically observable properties, not laboratory measurements.
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="py-1 pr-3">Indicator</th>
              <th className="py-1 pr-3">Observes</th>
              <th className="py-1 pr-3">Formula (Sentinel-2 bands)</th>
              <th className="py-1 pr-3">Range</th>
              <th className="py-1">Scientific basis and confounders</th>
            </tr>
          </thead>
          <tbody className="align-top">
            {m.indicators.map((ind) => (
              <tr key={ind.key} className="border-t">
                <td className="py-2 pr-3 font-medium">
                  {ind.display_name}
                  <div className="font-mono text-[10px] text-muted-foreground">{ind.key}</div>
                </td>
                <td className="py-2 pr-3">{ind.observes}</td>
                <td className="py-2 pr-3 font-mono text-[11px]">{ind.formula}</td>
                <td className="py-2 pr-3 font-mono text-[11px]">
                  {ind.valid_range[0]} … {ind.valid_range[1]}
                  {ind.units ? ` ${ind.units}` : ""}
                </td>
                <td className="py-2 text-muted-foreground">{ind.scientific_basis}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function MethodologyPage() {
  const q = useQuery({ queryKey: ["methodology"], queryFn: api.methodology, staleTime: Infinity });
  if (q.isPending) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>;
  if (q.isError || !q.data) return <div className="p-6 text-sm text-destructive">Could not load the methodology.</div>;
  const m = q.data;
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-6">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold">
            <BookOpen className="h-5 w-5 text-primary" /> Methodology
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">{m.product_boundary}</p>
          <div className="mt-3 flex flex-wrap items-center gap-1 text-xs">
            {m.workflow.map((w, i) => (
              <span key={w} className="flex items-center gap-1">
                <span className="rounded-full bg-primary/10 px-2.5 py-1 font-medium text-primary">{w}</span>
                {i < m.workflow.length - 1 && <ChevronRight className="h-3 w-3 text-muted-foreground" />}
              </span>
            ))}
          </div>
        </div>
        <StepCard step={m.data} />
        <StepCard step={m.water_detection} />
        <IndicatorTable m={m} />
        <StepCard step={m.temporal_monitoring} />
        <StepCard step={m.anomaly_detection} />
        <StepCard step={m.prioritisation} />
        <StepCard step={m.alerts} />
        <StepCard step={m.explainability} />
        <StepCard step={m.validation_loop} />
        <section className="rounded-lg border bg-card p-4">
          <h3 className="text-sm font-semibold">Known limitations</h3>
          <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
            {m.limitations.map((l, i) => (
              <li key={i} className="flex gap-1.5">
                <ChevronRight className="mt-0.5 h-3 w-3 shrink-0" />
                <span>{l}</span>
              </li>
            ))}
          </ul>
        </section>
        <Disclaimer text={m.disclaimer} />
      </div>
    </div>
  );
}
