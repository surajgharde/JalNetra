/**
 * Live IoT buoy telemetry for the Khadakwasla pilot (one physical device).
 * Renders nothing for every other water body -- this is a single-site pilot,
 * not a general per-lake feature yet.
 */
import { useEffect, useRef, useState } from "react";
import { Compass, Radio, RefreshCw, Sparkles, Wifi } from "lucide-react";
import { useIotAnalyze, useIotLive } from "@/api/hooks";
import type { IotAnalyzeResponse, WaterBodyDetail } from "@/api/types";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const KHADAKWASLA_ID = "wb_khadakwasla";

function isKhadakwasla(wb: WaterBodyDetail | null | undefined): boolean {
  if (!wb) return false;
  return wb.id === KHADAKWASLA_ID || wb.name.toLowerCase().includes("khadakwasla");
}

// --- status classification (Green -> Amber -> Red, always paired with a label) ---

type Tone = "good" | "warning" | "critical";

const TONE_BADGE: Record<Tone, string> = {
  good: "bg-emerald-100 text-emerald-800",
  warning: "bg-amber-100 text-amber-800",
  critical: "bg-red-100 text-red-800",
};

function tempStatus(v: number): { label: string; tone: Tone } {
  if (v <= 24) return { label: "Ideal", tone: "good" };
  if (v <= 28) return { label: "Moderate", tone: "warning" };
  return { label: "Stress", tone: "critical" };
}

function tdsStatus(v: number): { label: string; tone: Tone } {
  if (v <= 300) return { label: "Excellent", tone: "good" };
  if (v <= 600) return { label: "Good", tone: "good" };
  if (v <= 900) return { label: "Fair", tone: "warning" };
  return { label: "Unsafe", tone: "critical" };
}

function turbidityStatus(v: number): { label: string; tone: Tone } {
  if (v > 3.8) return { label: "Crystal Clear", tone: "good" };
  if (v >= 2.5) return { label: "Moderate", tone: "warning" };
  return { label: "Highly Turbid", tone: "critical" };
}

/** A horizontal green->amber->red (or reversed) range gauge with a pointer at
 * the current value. `stops` are CSS gradient stops (0-100, left to right);
 * the pointer position is computed from `value` against [min, max] -- never
 * the same thing as the color stops, so an inverted metric (turbidity
 * voltage, where higher is better) can still colour low correctly. */
function GaugeBar({
  value,
  min,
  max,
  stops,
}: {
  value: number;
  min: number;
  max: number;
  stops: string; // e.g. "#ef4444 0%, #f59e0b 50%, #10b981 100%"
}) {
  const pct = Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
  return (
    <div className="relative h-2 w-full rounded-full" style={{ backgroundImage: `linear-gradient(to right, ${stops})` }}>
      <div
        className="absolute top-1/2 h-3.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-sm bg-foreground ring-2 ring-card"
        style={{ left: `${pct}%` }}
        aria-hidden
      />
    </div>
  );
}

function MetricRow({
  icon,
  label,
  value,
  sub,
  status,
  gauge,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
  status: { label: string; tone: Tone };
  gauge: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
          {icon}
          {label}
        </span>
        <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold", TONE_BADGE[status.tone])}>
          {status.label}
        </span>
      </div>
      <div className="flex items-baseline gap-2">
        <span className="text-lg font-semibold tabular-nums">{value}</span>
        {sub && <span className="text-[11px] text-muted-foreground">{sub}</span>}
      </div>
      {gauge}
    </div>
  );
}

function AnalysisCards({ result }: { result: IotAnalyzeResponse }) {
  return (
    <div className="grid gap-2 md:grid-cols-3">
      <div className="rounded-lg border bg-card p-3">
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          🌊 Condition assessment
        </h4>
        <p className="text-xs leading-relaxed">{result.condition_summary}</p>
        <dl className="mt-2 space-y-1 text-[11px] text-muted-foreground">
          <div>
            <dt className="font-medium text-foreground">Temperature</dt>
            <dd>{result.parameters.temperature_analysis}</dd>
          </div>
          <div>
            <dt className="font-medium text-foreground">TDS</dt>
            <dd>{result.parameters.tds_analysis}</dd>
          </div>
          <div>
            <dt className="font-medium text-foreground">Turbidity</dt>
            <dd>{result.parameters.turbidity_analysis}</dd>
          </div>
        </dl>
      </div>
      <div className="rounded-lg border bg-card p-3">
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          🛠️ Actionable suggestions
        </h4>
        <ul className="space-y-1 text-xs leading-relaxed">
          {result.suggestions.map((s, i) => (
            <li key={i} className="flex gap-1.5">
              <span className="text-muted-foreground">{i + 1}.</span>
              {s}
            </li>
          ))}
        </ul>
      </div>
      <div className="rounded-lg border bg-card p-3">
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          🔮 Predictive forecast
        </h4>
        <p className="text-xs leading-relaxed">{result.future_prediction}</p>
      </div>
    </div>
  );
}

export function IotSensorCard({ waterBody }: { waterBody: WaterBodyDetail | null | undefined }) {
  const active = isKhadakwasla(waterBody);
  const live = useIotLive(active);
  const analyze = useIotAnalyze();
  const autoRanFor = useRef<string | null>(null);
  const [showAnalysis, setShowAnalysis] = useState(false);

  const data = live.data?.data ?? null;
  const online = live.data?.online === true;

  function runAnalysis() {
    if (!data) return;
    setShowAnalysis(true);
    analyze.mutate({ water_body_name: waterBody?.name ?? "Khadakwasla Dam", telemetry: data });
  }

  // Auto-run once per "just came online" transition, not on every 5 s poll.
  useEffect(() => {
    if (!online || !data || !active) return;
    const key = waterBody?.id ?? "khadakwasla";
    if (autoRanFor.current === key) return;
    autoRanFor.current = key;
    runAnalysis();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online, active, waterBody?.id]);

  if (!active) return null;

  const temp = data?.temperature?.value;
  const tds = data?.tds?.value;
  const turb = data?.turbidity?.value;

  return (
    <section className="space-y-3 rounded-lg border bg-card p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Radio className="h-4 w-4 text-primary" /> Khadakwasla IoT Telemetry
          <span className="text-xs font-normal text-muted-foreground">(Buoy #01)</span>
        </h3>
        {online ? (
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="flex items-center gap-1 font-medium text-emerald-700">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
              </span>
              LIVE
            </span>
            <span className="flex items-center gap-1">
              <Wifi className="h-3 w-3" /> {data?.wifi?.rssi ?? "—"} dBm
            </span>
            <span>Device: {data?.device ?? "—"}</span>
          </span>
        ) : (
          <span className="flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-[11px] font-semibold text-red-800">
            ● OFFLINE
          </span>
        )}
      </div>

      {!online ? (
        <div className="flex flex-col items-start gap-2 rounded-md border border-dashed bg-muted/40 p-3">
          <p className="text-xs text-muted-foreground">
            IoT Sensor Buoy at Khadakwasla Dam is currently unreachable at 192.168.137.204. Ensure the
            ESP32/buoy hardware is powered and connected to the hotspot/network.
          </p>
          <Button size="sm" variant="outline" disabled={live.isFetching} onClick={() => void live.refetch()}>
            <RefreshCw className={cn("h-3.5 w-3.5", live.isFetching && "animate-spin")} /> Check connection
          </Button>
        </div>
      ) : (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            {typeof temp === "number" && (
              <MetricRow
                icon={<Compass className="h-3.5 w-3.5" />}
                label="Water Temperature"
                value={`${temp.toFixed(2)} °C`}
                status={tempStatus(temp)}
                gauge={
                  <GaugeBar
                    value={temp}
                    min={15}
                    max={32}
                    stops="#10b981 0%, #10b981 52.9%, #f59e0b 52.9%, #f59e0b 76.5%, #ef4444 76.5%, #ef4444 100%"
                  />
                }
              />
            )}
            {typeof tds === "number" && (
              <MetricRow
                icon={<Compass className="h-3.5 w-3.5" />}
                label="Total Dissolved Solids"
                value={`${tds.toFixed(1)} ppm`}
                sub={`ADC: ${data?.tds?.adc ?? "—"}, ${(data?.tds?.voltage ?? 0).toFixed(3)} V`}
                status={tdsStatus(tds)}
                gauge={
                  <GaugeBar
                    value={tds}
                    min={0}
                    max={1200}
                    stops="#10b981 0%, #10b981 25%, #eab308 25%, #eab308 50%, #f59e0b 50%, #f59e0b 75%, #ef4444 75%, #ef4444 100%"
                  />
                }
              />
            )}
            {typeof turb === "number" && (
              <MetricRow
                icon={<Compass className="h-3.5 w-3.5" />}
                label="Turbidity"
                value={`${turb.toFixed(3)} V`}
                sub={`ADC: ${data?.turbidity?.adc ?? "—"}, status: ${data?.turbidity?.status ?? "unknown"}`}
                status={turbidityStatus(turb)}
                gauge={
                  <GaugeBar
                    value={turb}
                    min={0}
                    max={5}
                    stops="#ef4444 0%, #ef4444 50%, #f59e0b 50%, #f59e0b 76%, #10b981 76%, #10b981 100%"
                  />
                }
              />
            )}
          </div>

          <div className="border-t pt-3">
            <Button
              size="sm"
              className="bg-gradient-to-r from-primary to-sky-600 text-primary-foreground hover:opacity-90"
              disabled={analyze.isPending || !data}
              onClick={runAnalysis}
            >
              <Sparkles className="h-3.5 w-3.5" />
              {analyze.isPending ? "Analyzing…" : "Run AI Water Analysis"}
            </Button>
            {analyze.error && (
              <p className="mt-2 text-xs text-destructive">Could not analyze: {analyze.error.message}</p>
            )}
            {showAnalysis && analyze.data && (
              <div className="mt-3">
                <AnalysisCards result={analyze.data} />
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
