import type { Severity } from "@/api/types";
import { severityColor } from "@/lib/format";

/** SVG ring: arc length = priority / 100, colour = severity, confidence below. */
export function PriorityRing({
  score,
  severity,
  confidence,
  size = 96,
}: {
  score: number;
  severity: Severity;
  confidence: number;
  size?: number;
}) {
  const r = (size - 12) / 2;
  const c = 2 * Math.PI * r;
  const dash = (Math.max(0, Math.min(100, score)) / 100) * c;
  const colour = severityColor[severity];
  return (
    <div className="flex flex-col items-center" aria-label={`priority ${Math.round(score)} of 100`}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#e2e8f0" strokeWidth={8} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={colour}
          strokeWidth={8}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${c - dash}`}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
        <text x="50%" y="46%" textAnchor="middle" fontSize={size / 4} fontWeight={700} fill="#0f172a">
          {Math.round(score)}
        </text>
        <text x="50%" y="66%" textAnchor="middle" fontSize={size / 9} fill="#64748b">
          priority
        </text>
      </svg>
      <div className="text-[11px] text-muted-foreground">
        confidence <span className="font-mono font-medium text-foreground">{confidence.toFixed(2)}</span>
      </div>
    </div>
  );
}
