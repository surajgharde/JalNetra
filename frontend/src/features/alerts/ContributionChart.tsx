import { Bar, BarChart, Cell, LabelList, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { ContributionOut } from "@/api/types";
import { fmtSigned } from "@/lib/format";

/**
 * The four signed contributions (score points / 100). Negative bars render
 * in red: rainfall discounting an alert is the most persuasive thing the
 * system does, and it must be visible.
 */
export function ContributionChart({ contributions }: { contributions: ContributionOut[] }) {
  const rows = contributions.map((c) => ({ ...c, label: c.factor }));
  const span = Math.max(0.1, ...rows.map((r) => Math.abs(r.value)));
  return (
    <div className="h-40 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 36, left: 8, bottom: 4 }}>
          <XAxis type="number" domain={[-span * 1.3, span * 1.3]} fontSize={10} tickFormatter={(v: number) => v.toFixed(2)} />
          <YAxis type="category" dataKey="label" width={170} fontSize={10} tick={{ fill: "#334155" }} />
          <ReferenceLine x={0} stroke="#64748b" />
          <Tooltip
            formatter={(v: unknown) => [fmtSigned(v as number), "score points / 100"]}
            labelFormatter={(l) => String(l)}
          />
          <Bar dataKey="value" isAnimationActive={false} radius={2}>
            {rows.map((r) => (
              <Cell key={r.key ?? r.label} fill={r.value < 0 ? "#dc2626" : "#2563eb"} />
            ))}
            <LabelList dataKey="value" position="right" fontSize={10} formatter={(v: unknown) => fmtSigned(v as number)} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
