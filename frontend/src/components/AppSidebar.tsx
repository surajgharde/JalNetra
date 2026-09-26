import type { ReactNode } from "react";
import {
  BookOpen,
  Clock,
  Droplets,
  LayoutDashboard,
  LineChart,
  ListOrdered,
  Star,
  Table2,
  X,
} from "lucide-react";
import { NavLink } from "react-router-dom";
import { formatDistanceToNowStrict } from "date-fns";
import { useHealth, useRecentHistory, useRemoveFromWishlist, useWaterBodies, useWishlist } from "@/api/hooks";
import type { RecentItem, WishlistItemOut } from "@/api/types";
import { STATUS_DOT, fmtKm2, statusLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

/**
 * API reachability, parked at the foot of the rail. "Degraded" and
 * "unreachable" are different states: the API answers 503 with per-service
 * detail when an upstream (Earth Search, Earth Engine) is down but the app
 * itself is serving, and saying "unreachable" there sends people hunting the
 * wrong fault. Red is reserved for no answer at all.
 */
function HealthDot() {
  const { data, isError } = useHealth();
  const down = data ? Object.entries(data.services).filter(([, s]) => s.status !== "ok") : [];
  const tone = isError ? "bad" : !data ? "unknown" : down.length ? "degraded" : "ok";
  const label =
    tone === "bad"
      ? "API unreachable"
      : tone === "unknown"
        ? "checking…"
        : tone === "degraded"
          ? `API degraded · ${down.map(([n]) => n).join(", ")}`
          : "API ok";
  const title =
    tone === "degraded"
      ? down.map(([n, s]) => `${n}: ${s.error ?? "error"}`).join("\n")
      : label;
  return (
    <span className="flex items-start gap-1.5 text-xs text-muted-foreground" title={title}>
      <span
        className={cn(
          "mt-1 h-2 w-2 shrink-0 rounded-full",
          tone === "bad" ? "bg-red-500" : tone === "ok" ? "bg-emerald-500" : "bg-amber-500",
        )}
      />
      <span className="min-w-0 break-words">{label}</span>
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

/** One row shared by the wishlist and recent-history sections: a status dot,
 * the label, a status/area caption, and an optional trailing slot (remove
 * button, or a relative timestamp). Clicking opens the quick-look drawer
 * (S14) rather than switching the whole dashboard's selected water body. */
function SidebarRow({
  label,
  caption,
  status,
  onClick,
  trailing,
}: {
  label: string;
  caption: string;
  status: "alert" | "watch" | "normal" | "baseline_building" | "no_data";
  onClick: () => void;
  trailing?: ReactNode;
}) {
  return (
    <div className="group flex items-center gap-1.5 rounded px-2 py-1.5 hover:bg-accent">
      <button onClick={onClick} className="flex min-w-0 flex-1 items-center gap-1.5 text-left">
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", STATUS_DOT[status])} />
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-xs">{label}</span>
          <span className="block truncate text-[10px] text-muted-foreground">{caption}</span>
        </span>
      </button>
      {trailing}
    </div>
  );
}

function WishlistSection() {
  const { data } = useWishlist();
  const openDetails = useUi((s) => s.openDetails);
  const remove = useRemoveFromWishlist();
  const items = data?.items ?? [];
  if (!items.length) return null;

  return (
    <div className="border-t pt-2">
      <div className="flex items-center justify-between px-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        <span>Wishlist</span>
        <Star className="h-3 w-3 text-amber-500" />
      </div>
      <div className="mt-1 space-y-0.5">
        {items.map((item: WishlistItemOut) => (
          <SidebarRow
            key={item.id}
            label={item.custom_name}
            caption={`${item.water_body.district} · ${fmtKm2(item.water_body.area_km2)} · ${statusLabel[item.water_body.status] ?? item.water_body.status}`}
            status={item.water_body.status}
            onClick={() => openDetails(item.water_body_id)}
            trailing={
              <button
                className="shrink-0 rounded p-0.5 opacity-0 hover:bg-muted group-hover:opacity-100"
                title="Remove from wishlist"
                disabled={remove.isPending}
                onClick={(e) => {
                  e.stopPropagation();
                  remove.mutate(item.id);
                }}
              >
                <X className="h-3 w-3 text-muted-foreground" />
              </button>
            }
          />
        ))}
      </div>
    </div>
  );
}

function RecentHistorySection() {
  const { data } = useRecentHistory(10);
  const openDetails = useUi((s) => s.openDetails);
  const items = data?.items ?? [];
  if (!items.length) return null;

  return (
    <div className="border-t pt-2">
      <div className="flex items-center justify-between px-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        <span>Recent history</span>
        <Clock className="h-3 w-3 text-primary" />
      </div>
      <div className="mt-1 space-y-0.5">
        {items.map((item: RecentItem) => (
          <SidebarRow
            key={item.water_body_id}
            label={item.water_body.name}
            caption={`${formatDistanceToNowStrict(new Date(item.last_viewed_at), { addSuffix: true })}${item.wishlisted ? " · saved" : ""}`}
            status={item.water_body.status}
            onClick={() => openDetails(item.water_body_id)}
          />
        ))}
      </div>
    </div>
  );
}

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

        <WishlistSection />
        <RecentHistorySection />
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
