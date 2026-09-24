import { RASTER_LAYERS } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";
import { LiveImageryControls } from "./LiveImagery";

export function LayerToggles({ disabled }: { disabled: boolean }) {
  const layers = useUi((s) => s.layers);
  const toggle = useUi((s) => s.toggleLayer);
  const showAlerts = useUi((s) => s.showAlerts);
  const setShowAlerts = useUi((s) => s.setShowAlerts);
  const showZones = useUi((s) => s.showZones);
  const setShowZones = useUi((s) => s.setShowZones);

  return (
    <div className="pointer-events-auto min-h-0 w-52 overflow-y-auto rounded-md border bg-card/95 p-2 text-xs shadow">
      <label className="flex items-center gap-2 py-0.5">
        <input type="checkbox" checked={showAlerts} onChange={(e) => setShowAlerts(e.target.checked)} />
        Open alerts
      </label>
      <label className="flex items-center gap-2 py-0.5">
        <input type="checkbox" checked={showZones} onChange={(e) => setShowZones(e.target.checked)} />
        Zones
      </label>
      <div className={cn("mt-1 border-t pt-1", disabled && "opacity-50")}>
        <div className="mb-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
          Rasters {disabled && "(pick a date)"}
        </div>
        {RASTER_LAYERS.map((l) => (
          <label key={l.key} className="flex items-center gap-2 py-0.5">
            <input
              type="checkbox"
              disabled={disabled}
              checked={layers[l.key]}
              onChange={() => toggle(l.key)}
            />
            <span className="h-2.5 w-6 rounded-sm" style={{ background: l.swatch }} />
            {l.label}
          </label>
        ))}
      </div>
      <LiveImageryControls />
    </div>
  );
}
