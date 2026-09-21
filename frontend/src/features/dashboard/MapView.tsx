import { useEffect, useMemo } from "react";
import L from "leaflet";
import { GeoJSON, MapContainer, TileLayer, useMap } from "react-leaflet";
import type { Feature as GJFeature, FeatureCollection as GJFeatureCollection, Geometry as GJGeometry } from "geojson";
import { useAlertsGeo, useWaterBody } from "@/api/hooks";
import { api } from "@/api/client";
import { Disclaimer } from "@/components/Disclaimer";
import { RASTER_LAYERS, severityColor } from "@/lib/format";
import { useUi } from "@/store/ui";
import { LayerToggles } from "./LayerToggles";

const MAHARASHTRA_CENTER: [number, number] = [18.9, 75.5];

function FitToBody({ bbox }: { bbox: number[] | undefined }) {
  const map = useMap();
  useEffect(() => {
    if (!bbox) return;
    const [minx, miny, maxx, maxy] = bbox;
    map.fitBounds(
      [
        [miny, minx],
        [maxy, maxx],
      ],
      { padding: [24, 24] },
    );
  }, [bbox?.join(","), map]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

/** Raster chip layers for the selected body + date, one L.tileLayer per visible layer. */
function RasterLayers({ waterBodyId, date }: { waterBodyId: string; date: string }) {
  const layers = useUi((s) => s.layers);
  return (
    <>
      {RASTER_LAYERS.filter((l) => layers[l.key]).map((l) => (
        <TileLayer
          key={`${l.key}-${date}`}
          url={api.tiles.template(l.key, waterBodyId, date)}
          opacity={l.key === "watermask" ? 0.5 : 0.85}
          maxNativeZoom={16}
          maxZoom={19}
          zIndex={l.key === "watermask" ? 300 : 350}
        />
      ))}
    </>
  );
}

export function MapView() {
  const waterBodyId = useUi((s) => s.waterBodyId);
  const date = useUi((s) => s.date);
  const zoneId = useUi((s) => s.zoneId);
  const selectZone = useUi((s) => s.selectZone);
  const showAlerts = useUi((s) => s.showAlerts);
  const showZones = useUi((s) => s.showZones);
  const openAlert = useUi((s) => s.openAlert);
  const body = useWaterBody(waterBodyId);
  const alerts = useAlertsGeo({ status: "active" });

  const boundary = useMemo<GJFeature<GJGeometry> | null>(
    () =>
      body.data
        ? { type: "Feature", properties: {}, geometry: body.data.boundary as unknown as GJGeometry }
        : null,
    [body.data],
  );
  const zones = body.data?.zones as unknown as GJFeatureCollection | undefined;

  return (
    <div className="relative h-full w-full">
      <MapContainer center={MAHARASHTRA_CENTER} zoom={7} className="h-full w-full" preferCanvas>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        <FitToBody bbox={body.data?.bbox} />
        {boundary && (
          <GeoJSON
            key={`b-${body.data?.id}`}
            data={boundary}
            style={{ color: "#0c4a6e", weight: 2, fillOpacity: 0.04 }}
          />
        )}
        {showZones && zones && (
          <GeoJSON
            key={`z-${body.data?.id}-${zoneId ?? ""}`}
            data={zones}
            style={(f) => ({
              color: f?.properties?.id === zoneId ? "#0369a1" : "#64748b",
              weight: f?.properties?.id === zoneId ? 2.5 : 1,
              dashArray: "4 3",
              fillOpacity: f?.properties?.id === zoneId ? 0.12 : 0.02,
            })}
            onEachFeature={(f, layer) => {
              const p = f.properties as { id: string; name: string; baseline_status: string; open_alert_id: string | null };
              layer.bindTooltip(`${p.name} · baseline ${p.baseline_status}`, { sticky: true });
              layer.on("click", () => selectZone(p.id === zoneId ? null : p.id));
            }}
          />
        )}
        {waterBodyId && date && <RasterLayers waterBodyId={waterBodyId} date={date} />}
        {showAlerts && alerts.data && (
          <GeoJSON
            key={`a-${alerts.data.features.length}`}
            data={alerts.data as unknown as GJFeatureCollection}
            style={(f) => {
              const sev = (f?.properties?.severity ?? "low") as keyof typeof severityColor;
              return { color: severityColor[sev], weight: 2, fillColor: severityColor[sev], fillOpacity: 0.25 };
            }}
            onEachFeature={(f, layer) => {
              const p = f.properties as {
                alert_id: string;
                water_body_name: string;
                zone_name: string;
                severity: string;
                priority_score: number;
                observed_on: string;
              };
              layer.bindTooltip(
                `<b>${p.water_body_name}</b> · ${p.zone_name}<br/>${p.severity.toUpperCase()} · priority ${Math.round(p.priority_score)} · ${p.observed_on}`,
                { sticky: true },
              );
              layer.on("click", () => openAlert(p.alert_id));
              if (p.severity === "high" && layer instanceof L.Path) layer.setStyle({ weight: 3 });
            }}
          />
        )}
      </MapContainer>
      <div className="absolute right-3 top-3 z-[1000]">
        <LayerToggles disabled={!waterBodyId || !date} />
      </div>
      {alerts.data && (
        <div className="absolute bottom-3 left-3 z-[1000] max-w-md">
          <Disclaimer text={alerts.data.disclaimer} className="bg-amber-50/95 shadow" />
        </div>
      )}
    </div>
  );
}
