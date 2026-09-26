import { useEffect, useRef } from "react";
import { Route, Routes, useLocation, useSearchParams } from "react-router-dom";
import { useObservations, useTouchRecent } from "@/api/hooks";
import { AppSidebar } from "@/components/AppSidebar";
import { EmptyState } from "@/components/States";
import { Toaster } from "@/components/Toaster";
import { AlertQueue } from "@/features/alerts/AlertQueue";
import { AlertSheet } from "@/features/alerts/AlertSheet";
import { IndicatorsPage } from "@/features/dashboard/IndicatorsPage";
import { Overview } from "@/features/dashboard/Overview";
import { TrendsPage } from "@/features/dashboard/TrendsPage";
import { WaterBodyBar } from "@/features/dashboard/WaterBodyBar";
import { LakeDetailsDrawer } from "@/features/lake/LakeDetailsDrawer";
import { MethodologyPage } from "@/features/methodology/MethodologyPage";
import { useUi } from "@/store/ui";

/** Keep `?wb=` and `?alert=` in the URL so a demo state is a shareable link. */
function UrlSync() {
  const [params, setParams] = useSearchParams();
  const { pathname } = useLocation();
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
  }, [waterBodyId, alertId, pathname]);
  return null;
}

/**
 * Records a "recently inspected" entry (S14) whenever a water body becomes
 * the dashboard's selection or is opened in the quick-look drawer. One place
 * for this rather than scattering `touch` calls through every entry point
 * (the water-body bar, alert links, the drawer's own "view dashboard" button).
 */
function RecentHistorySync() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const detailsWaterBodyId = useUi((s) => s.detailsWaterBodyId);
  const touch = useTouchRecent();
  const last = useRef<string | null>(null);

  useEffect(() => {
    const id = detailsWaterBodyId ?? waterBodyId;
    if (!id || id === last.current) return;
    last.current = id;
    touch.mutate(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waterBodyId, detailsWaterBodyId]);
  return null;
}

/**
 * The selected scene drives the map rasters and the indicator tables, so it is
 * defaulted here rather than inside the timeline — which now lives on its own
 * screen and may never be opened.
 */
function DateSync() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const date = useUi((s) => s.date);
  const selectDate = useUi((s) => s.selectDate);
  const { data } = useObservations(waterBodyId);

  useEffect(() => {
    if (date || !data) return;
    const usable = data.items.filter((o) => o.usable);
    // The API returns newest first; the freshest usable scene is the default.
    if (usable.length) selectDate(usable[0].observed_on);
  }, [date, data, selectDate]);
  return null;
}

export default function App() {
  return (
    <div className="flex h-full">
      <AppSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <WaterBodyBar />
        <main className="min-h-0 flex-1">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/indicators" element={<IndicatorsPage />} />
            <Route path="/trends" element={<TrendsPage />} />
            <Route path="/alerts" element={<AlertQueue />} />
            <Route path="/methodology" element={<MethodologyPage />} />
            {/* A mistyped or stale link rendered a blank frame; say so instead. */}
            <Route
              path="*"
              element={
                <EmptyState
                  title="Page not found"
                  hint="That address does not exist. Pick a section from the left."
                />
              }
            />
          </Routes>
        </main>
      </div>
      <UrlSync />
      <DateSync />
      <RecentHistorySync />
      <AlertSheet />
      <LakeDetailsDrawer />
      <Toaster />
    </div>
  );
}
