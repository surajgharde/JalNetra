import type { ReactNode } from "react";
import { ChevronLeft } from "lucide-react";
import { Link } from "react-router-dom";

/** Header for a drill-down screen: a way back, what you are looking at, controls. */
export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 border-b bg-card px-3 py-2">
      <Link
        to="/"
        className="flex shrink-0 items-center gap-1 rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
      >
        <ChevronLeft className="h-3.5 w-3.5" />
        Overview
      </Link>
      <div className="min-w-0 leading-tight">
        <div className="truncate text-sm font-semibold">{title}</div>
        {subtitle && <div className="truncate text-[11px] text-muted-foreground">{subtitle}</div>}
      </div>
      {actions && <div className="ml-auto flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
