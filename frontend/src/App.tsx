import { useEffect } from "react";
import { Activity, BookOpen, Droplets, ListOrdered } from "lucide-react";
import { NavLink, Route, Routes, useSearchParams } from "react-router-dom";
import { useHealth } from "@/api/hooks";
import { AlertQueue } from "@/features/alerts/AlertQueue";
import { AlertSheet } from "@/features/alerts/AlertSheet";
import { IndicatorPanel } from "@/features/dashboard/IndicatorPanel";
import { MapView } from "@/features/dashboard/MapView";
import { SeriesChart } from "@/features/dashboard/SeriesChart";
import { TimelineScrubber } from "@/features/dashboard/TimelineScrubber";
import { WaterBodyList } from "@/features/dashboard/WaterBodyList";
import { PipelineRunner } from "@/features/jobs/PipelineRunner";
import { MethodologyPage } from "@/features/methodology/MethodologyPage";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

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

/** Keep `?wb=` and `?alert=` in the URL so a demo state is a shareable link. */
function UrlSync() {
  const [params, setParams] = useSearchParams();
  const waterBodyId = useUi((s) => s.waterBodyId);
  const alertId = useUi((s) => s.alertId);
  const selectWaterBody = useUi((s) => s.selectWaterBody);
  const openAlert = useUi((s) => s.openAlert);

  useEffect(() => {
    const wb = params.get("wb");
    const al = params.get("alert");
    if (wb && wb !== waterBodyId) selectWaterBody(wb);
    if (al && al !== alertId) openAlert(al);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const next = new URLSearchParams(params);
    if (waterBodyId) next.set("wb", waterBodyId);
    else next.delete("wb");
    if (alertId) next.set("alert", alertId);
    else next.delete("alert");
    if (next.toString() !== params.toString()) setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waterBodyId, alertId]);
  return null;
}

function Dashboard() {
  return (
    <div className="grid h-full min-h-0 grid-cols-[17rem_minmax(0,1fr)_23rem]">
      <aside className="min-h-0 min-w-0 border-r bg-card">
        <WaterBodyList />
      </aside>
      <section className="grid min-h-0 min-w-0 grid-rows-[minmax(0,1fr)_auto_clamp(8rem,22vh,14rem)]">
        <div className="min-h-0 min-w-0">
          <MapView />
        </div>
        <div className="min-w-0 border-t bg-card">
          <TimelineScrubber />
        </div>
        <div className="min-h-0 min-w-0 border-t bg-card">
          <SeriesChart />
        </div>
      </section>
      <aside className="min-h-0 min-w-0 border-l bg-card">
        <IndicatorPanel />
      </aside>
    </div>
  );
}

const navClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    "flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm",
    isActive ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent",
  );

export default function App() {
  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-4 border-b bg-card px-4 py-2">
        <div className="flex items-center gap-2">
          <Droplets className="h-5 w-5 text-primary" />
          <span className="text-base font-semibold">JalNetra</span>
          <span className="hidden text-xs text-muted-foreground md:inline">
            satellite water-quality anomaly intelligence · Maharashtra
          </span>
        </div>
        <nav className="flex items-center gap-1">
          <NavLink to="/" end className={navClass}>
            <Activity className="h-4 w-4" /> Dashboard
          </NavLink>
          <NavLink to="/alerts" className={navClass}>
            <ListOrdered className="h-4 w-4" /> Priority queue
          </NavLink>
          <NavLink to="/methodology" className={navClass}>
            <BookOpen className="h-4 w-4" /> Methodology
          </NavLink>
        </nav>
        <div className="ml-auto flex items-center gap-4">
          <PipelineRunner />
          <HealthDot />
        </div>
      </header>
      <main className="min-h-0 flex-1">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/alerts" element={<AlertQueue />} />
          <Route path="/methodology" element={<MethodologyPage />} />
        </Routes>
      </main>
      <UrlSync />
      <AlertSheet />
    </div>
  );
}
