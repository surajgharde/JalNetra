import { useMemo } from "react";
import { format, parseISO, subDays } from "date-fns";
import {
  Area,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useSeries, useWaterBody } from "@/api/hooks";
import { EmptyState, ErrorState, PanelSkeleton } from "@/components/States";
import { fmtDate, fmtNum, indicatorLabel, indicatorShortLabel, QUALITY_INDICATORS } from "@/lib/format";
import { useUi } from "@/store/ui";

type Row = {
  t: number;
  date: string;
  value: number | null;
  band: [number, number] | null;
  median: number | null;
  z: number | null;
  flagged: number | null;
  scene_id: string;
};

/** Zone series for the selected indicator with its seasonal p10–p90 band. */
export function SeriesChart() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const zoneId = useUi((s) => s.zoneId);
  const date = useUi((s) => s.date);
  const indicator = useUi((s) => s.indicator);
  const selectIndicator = useUi((s) => s.selectIndicator);
  const selectDate = useUi((s) => s.selectDate);
  const body = useWaterBody(waterBodyId);
  const to = date ?? undefined;
  const from = to ? format(subDays(parseISO(to), 365), "yyyy-MM-dd") : undefined;
  const zone = zoneId ?? body.data?.zones.features[0]?.id ?? undefined;
  const { data, isLoading, error, refetch } = useSeries(waterBodyId, indicator, zone, from, to);

  const rows = useMemo<Row[]>(
    () =>
      (data?.points ?? []).map((p) => ({
        t: parseISO(p.observed_at).getTime(),
        date: p.observed_at,
        value: p.value ?? null,
        band:
          p.baseline_p10 !== null && p.baseline_p10 !== undefined && p.baseline_p90 !== null && p.baseline_p90 !== undefined
            ? [p.baseline_p10, p.baseline_p90]
            : null,
        median: p.baseline_mean ?? null,
        z: p.z_score ?? null,
        flagged: p.z_score !== null && p.z_score !== undefined && Math.abs(p.z_score) > 3 ? (p.value ?? null) : null,
        scene_id: p.scene_id,
      })),
    [data],
  );

  if (!waterBodyId) return null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-3 border-b px-3 py-1.5 text-xs">
        <div className="flex shrink-0 items-center gap-1">
          {QUALITY_INDICATORS.map((k) => (
            <button
              key={k}
              onClick={() => selectIndicator(k)}
              title={indicatorLabel(k)}
              className={`shrink-0 whitespace-nowrap rounded px-2 py-0.5 ${indicator === k ? "bg-primary text-primary-foreground" : "hover:bg-accent"}`}
            >
              {indicatorShortLabel(k)}
            </button>
          ))}
        </div>
        <div className="min-w-0 truncate text-right text-muted-foreground">
          {data && (
            <>
              {data.zone_id} · baseline {data.baseline_status}
              {data.baseline_status !== "usable" && ` (${data.baseline_usable_windows}/366 windows)`}
            </>
          )}
        </div>
      </div>
      <div className="min-h-0 flex-1 p-2">
        {isLoading && <PanelSkeleton rows={3} />}
        {error && <ErrorState error={error} onRetry={() => void refetch()} />}
        {data && rows.length === 0 && (
          <EmptyState title="No observations in this window" hint="Try another zone or indicator." />
        )}
        {rows.length > 0 && (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={rows} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(t: number) => format(new Date(t), "MMM yy")}
                fontSize={10}
              />
              <YAxis fontSize={10} width={40} domain={["auto", "auto"]} />
              <Tooltip
                labelFormatter={(t) => fmtDate(new Date(Number(t)).toISOString())}
                formatter={(v: unknown, name: unknown): [string, string] => {
                  if (name === "band" && Array.isArray(v)) return [`${fmtNum(v[0])} – ${fmtNum(v[1])}`, "seasonal p10–p90"];
                  if (name === "median") return [fmtNum(v as number), "seasonal median"];
                  if (name === "value") return [fmtNum(v as number), indicatorLabel(indicator)];
                  if (name === "flagged") return [fmtNum(v as number), "|z| > 3"];
                  return [String(v), String(name)];
                }}
              />
              <Area dataKey="band" stroke="none" fill="#9ecae1" fillOpacity={0.45} isAnimationActive={false} connectNulls />
              <Line dataKey="median" stroke="#3182bd" dot={false} strokeWidth={1} isAnimationActive={false} connectNulls />
              <Line
                dataKey="value"
                stroke="#0f172a"
                strokeWidth={1.2}
                dot={{ r: 2 }}
                isAnimationActive={false}
                connectNulls
                activeDot={{
                  r: 5,
                  onClick: (_e: unknown, p: unknown) => {
                    const payload = (p as { payload?: Row }).payload;
                    if (payload) selectDate(payload.date.slice(0, 10));
                  },
                }}
              />
              <Scatter dataKey="flagged" fill="#dc2626" isAnimationActive={false} />
              {date && (
                <ReferenceLine x={parseISO(date).getTime()} stroke="#0369a1" strokeDasharray="3 3" />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
