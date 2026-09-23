import { ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";

/**
 * One headline number on the overview. Tone is a *status* signal (reserved for
 * state, never reused as a series colour) and is always carried by the dot plus
 * the wording underneath, never by colour alone.
 */
export type Tone = "neutral" | "good" | "warning" | "critical";

const TONE: Record<Tone, { dot: string; value: string }> = {
  neutral: { dot: "bg-slate-400", value: "text-foreground" },
  good: { dot: "bg-emerald-500", value: "text-foreground" },
  warning: { dot: "bg-amber-500", value: "text-amber-700" },
  critical: { dot: "bg-red-500", value: "text-red-700" },
};

export function StatTile({
  label,
  value,
  sub,
  tone = "neutral",
  to,
  cta,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: Tone;
  /** When set the whole tile is the button into the screen that explains it. */
  to?: string;
  cta?: string;
}) {
  const body = (
    <>
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", TONE[tone].dot)} />
        <span className="truncate">{label}</span>
      </div>
      {/* Proportional figures: tabular-nums only belongs in columns of numbers. */}
      <div className={cn("mt-1.5 truncate text-2xl font-semibold leading-none", TONE[tone].value)}>{value}</div>
      <div className="mt-1.5 flex items-end justify-between gap-2">
        <span className="truncate text-[11px] leading-tight text-muted-foreground" title={sub}>
          {sub}
        </span>
        {to && (
          <span className="flex shrink-0 items-center gap-0.5 text-[11px] font-medium text-primary">
            {cta ?? "Open"}
            <ChevronRight className="h-3 w-3" />
          </span>
        )}
      </div>
    </>
  );

  const shell = "min-w-0 rounded-lg border bg-card px-3 py-2.5 text-left";
  return to ? (
    <Link to={to} className={cn(shell, "block transition-colors hover:border-primary hover:bg-accent/50")}>
      {body}
    </Link>
  ) : (
    <div className={shell}>{body}</div>
  );
}
