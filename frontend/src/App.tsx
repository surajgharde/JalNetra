import { useEffect } from "react";
import { Route, Routes, useSearchParams } from "react-router-dom";
import { AppSidebar } from "@/components/AppSidebar";
import { AlertQueue } from "@/features/alerts/AlertQueue";
import { AlertSheet } from "@/features/alerts/AlertSheet";
import { IndicatorPanel } from "@/features/dashboard/IndicatorPanel";
import { MapView } from "@/features/dashboard/MapView";
import { SeriesChart } from "@/features/dashboard/SeriesChart";
import { TimelineScrubber } from "@/features/dashboard/TimelineScrubber";
import { WaterBodyBar } from "@/features/dashboard/WaterBodyBar";
import { MethodologyPage } from "@/features/methodology/MethodologyPage";
import { useUi } from "@/store/ui";

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
    <div className="grid h-full min-h-0 grid-cols-[minmax(0,1fr)_23rem]">
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

export default function App() {
  return (
    <div className="flex h-full">
      <AppSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <WaterBodyBar />
        <main className="min-h-0 flex-1">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/alerts" element={<AlertQueue />} />
            <Route path="/methodology" element={<MethodologyPage />} />
          </Routes>
        </main>
      </div>
      <UrlSync />
      <AlertSheet />
    </div>
  );
}
