import { BookOpen, Droplets, LayoutDashboard, LineChart, ListOrdered, Table2 } from "lucide-react";
import { NavLink } from "react-router-dom";
import { useHealth, useWaterBodies } from "@/api/hooks";
import { cn } from "@/lib/utils";

/** API reachability, parked at the foot of the rail. */
function HealthDot() {
  const { data, isError } = useHealth();
  const ok = data?.status === "ok";
  const label = isError ? "API unreachable" : data ? `API ${data.status}` : "checking…";
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted-foreground" title={label}>
      <span className={cn("h-2 w-2 rounded-full", isError ? "bg-red-500" : ok ? "bg-emerald-500" : "bg-amber-500")} />
      {label}
    </span>
  );
}

const NAV = [
  { to: "/", end: true, icon: LayoutDashboard, label: "Overview" },
  { to: "/indicators", end: false, icon: Table2, label: "Indicators" },
  { to: "/trends", end: false, icon: LineChart, label: "Trends" },
  { to: "/alerts", end: false, icon: ListOrdered, label: "Priority queue" },
  { to: "/methodology", end: false, icon: BookOpen, label: "Methodology" },
] as const;

/**
 * The app rail: brand, the five sections, and API health. Water-body
 * selection is deliberately not here — it is the context bar across the top,
 * because it scopes every screen rather than navigating between them.
 */
export function AppSidebar() {
  const bodies = useWaterBodies();
  // Already in cache for the top bar; no extra request just for the badge.
  const openAlerts = (bodies.data?.items ?? []).reduce((n, wb) => n + wb.open_alerts, 0);

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r bg-card">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <Droplets className="h-5 w-5 shrink-0 text-primary" />
        <div className="min-w-0 leading-tight">
          <div className="text-sm font-semibold">JalNetra</div>
          <div className="truncate text-[10px] text-muted-foreground">Maharashtra · water anomalies</div>
        </div>
      </div>

      <nav className="flex-1 space-y-0.5 overflow-y-auto p-2">
        {NAV.map(({ to, end, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-2 rounded-md px-2.5 py-2 text-sm",
                isActive ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent",
              )
            }
          >
            {({ isActive }) => (
              <>
                <Icon className="h-4 w-4 shrink-0" />
                <span className="flex-1 truncate">{label}</span>
                {to === "/alerts" && openAlerts > 0 && (
                  <span
                    className={cn(
                      "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
                      isActive ? "bg-primary-foreground/20 text-primary-foreground" : "bg-red-100 text-red-800",
                    )}
                    title={`${openAlerts} open alerts`}
                  >
                    {openAlerts}
                  </span>
                )}
              </>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="space-y-1 border-t p-3">
        <HealthDot />
        <p className="text-[10px] leading-snug text-muted-foreground">
          Satellite water-quality anomaly intelligence. Findings need ground validation.
        </p>
      </div>
    </aside>
  );
}
