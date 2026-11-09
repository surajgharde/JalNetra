import { useEffect, useMemo, useState } from "react";
import L from "leaflet";
import { Crosshair, Layers, X } from "lucide-react";
import { GeoJSON, MapContainer, TileLayer, useMap } from "react-leaflet";
import type { Feature as GJFeature, FeatureCollection as GJFeatureCollection, Geometry as GJGeometry } from "geojson";
import { useAlert, useAlertGeometry, useAlertsGeo, useWaterBody } from "@/api/hooks";
import { api } from "@/api/client";
import { Disclaimer } from "@/components/Disclaimer";
import { RASTER_LAYERS, fmtKm2, severityColor } from "@/lib/format";
import { useUi } from "@/store/ui";
import { LayerToggles } from "./LayerToggles";
import { LiveImageryLayer } from "./LiveImagery";
import { MapSearchOverlay, MapSearchWidget } from "./MapSearchWidget";

const MAHARASHTRA_CENTER: [number, number] = [18.9, 75.5];

function FitToBody({ bbox, enabled }: { bbox: number[] | undefined; enabled: boolean }) {
  const map = useMap();
  useEffect(() => {
    // A focused alert owns the viewport: fitting the whole lake on top of it
    // is what made a 1 km plume look like the entire water body was flagged.
    if (!enabled || !bbox) return;
    const [minx, miny, maxx, maxy] = bbox;
    map.fitBounds(
      [
        [miny, minx],
        [maxy, maxx],
      ],
      { padding: [24, 24] },
    );
  }, [bbox?.join(","), enabled, map]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

/** What kind of shape an alert's geometry is, straight from `geom_kind`. */
const GEOM_KIND: Record<string, { label: string; dashed: boolean; fill: number }> = {
  // A DBSCAN cluster of hot pixels found against the whole body's water.
  plume: { label: "Flagged plume", dashed: false, fill: 0.35 },
  // The hottest patch inside a zone that deviated from its own baseline.
  localised: { label: "Hottest patch in zone", dashed: false, fill: 0.3 },
  // No distinct patch: the geometry IS the zone. Drawn dashed and barely
  // filled, because presenting a quarter of the lake as a measured plume is
  // exactly what made every alert look lake-wide.
  zone: { label: "Zone-wide deviation · no distinct patch", dashed: true, fill: 0.08 },
};

/**
 * The flagged shape of the alert being viewed, drawn on top with the viewport
 * zoomed to *it*.
 *
 * Two things used to go wrong here. The map never fetched this geometry at all,
 * so an alert showed only as a hairline in the active-alerts layer seen from a
 * viewport fitted to the whole lake. And 41% of alerts had no plume to fetch:
 * their geometry was the entire zone, a quarter of the lake, because the
 * backend's spatial pass contrasts pixels against the *whole body* and so
 * missed any zone that merely deviated from its own baseline. That is fixed
 * upstream (a within-zone localisation pass); `geom_kind` says which of the
 * three kinds of shape came back, and the last one is still honestly
 * zone-sized, so it is drawn and labelled as a zone rather than as a plume.
 */
function AlertFocusLayer({ alertId, onSelect }: { alertId: string; onSelect: () => void }) {
  const map = useMap();
  const geo = useAlertGeometry(alertId);
  const feature = useMemo<GJFeature<GJGeometry> | null>(
    () =>
      geo.data?.geometry
        ? {
            type: "Feature",
            properties: {},
            geometry: geo.data.geometry as unknown as GJGeometry,
          }
        : null,
    [geo.data],
  );

  const kindKey = (geo.data?.properties?.geom_kind ?? "zone") as string;
  const kind = GEOM_KIND[kindKey] ?? GEOM_KIND.zone;

  useEffect(() => {
    if (!feature) return;
    const bounds = L.geoJSON(feature).getBounds();
    if (!bounds.isValid()) return;
    // A patch gets zoomed to. A whole zone does not pretend to be one, so it
    // keeps more of the lake in frame. maxZoom matches the chips'
    // maxNativeZoom (16) so a 0.1 km2 patch never outruns the imagery.
    const zoneWide = kindKey === "zone";
    map.fitBounds(bounds.pad(zoneWide ? 0.15 : 0.5), {
      maxZoom: zoneWide ? 14 : 16,
      padding: [24, 24],
    });
  }, [alertId, feature, kindKey, map]);

  if (!feature) return null;
  const sev = (geo.data?.properties?.severity ?? "high") as keyof typeof severityColor;
  const color = severityColor[sev] ?? severityColor.high;
  return (
    <>
      {/* Halo first: a wide translucent stroke so the shape stays findable
          against both the basemap and the indicator raster beneath it. */}
      <GeoJSON
        key={`focus-halo-${alertId}-${kindKey}`}
        data={feature}
        style={{ color: "#ffffff", weight: 9, opacity: 0.85, fill: false }}
        interactive={false}
      />
      {/* The fill takes the clicks. It has to: it covers the zone polygon, and
          a click falling through to that would call selectZone and clear the
          very highlight the user just clicked on. */}
      <GeoJSON
        key={`focus-${alertId}-${kindKey}`}
        data={feature}
        style={{
          color,
          weight: 3,
          dashArray: kind.dashed ? "6 4" : undefined,
          fillColor: color,
          fillOpacity: kind.fill,
        }}
        onEachFeature={(_f, layer) => {
          layer.bindTooltip(`${kind.label} — click for the alert`, { sticky: true });
          layer.on("click", onSelect);
        }}
      />
    </>
  );
}

/** Caption for the focused shape: says what is highlighted and how to drop it. */
function FocusBanner({ alertId, onClear }: { alertId: string; onClear: () => void }) {
  const alert = useAlert(alertId);
  const geo = useAlertGeometry(alertId);
  const props = geo.data?.properties as
    | { geom_kind?: string; area_km2?: number | null }
    | undefined;
  const kind = GEOM_KIND[props?.geom_kind ?? "zone"] ?? GEOM_KIND.zone;
  // `area_km2` is the drawn shape's own area. For a localised patch that is not
  // the alert's `affected_area_km2`, which stays the body-level plume area the
  // score was computed from and is null for these.
  const area = props?.area_km2 ?? null;
  return (
    <div className="pointer-events-auto absolute left-1/2 top-3 z-[1000] flex max-w-[min(92%,34rem)] -translate-x-1/2 items-center gap-2 rounded-md border bg-card/95 px-3 py-1.5 text-xs shadow">
      <Crosshair className="h-3.5 w-3.5 shrink-0 text-destructive" />
      <span className="min-w-0 truncate">
        {kind.label}
        {alert.data && (
          <>
            {" · "}
            <b>{alert.data.zone.name}</b>
            {area !== null && ` · ${fmtKm2(area)}`}
            {" · "}
            {alert.data.severity}
          </>
        )}
      </span>
      <button
        onClick={onClear}
        className="ml-auto shrink-0 rounded px-1.5 py-0.5 font-medium text-muted-foreground hover:bg-accent hover:text-foreground"
        title="Clear the highlight and fit the whole water body"
      >
        Clear
      </button>
    </div>
  );
}

/** Raster chip layers for the selected body + date, one L.tileLayer per visible layer. */
function RasterLayers({
  waterBodyId,
  date,
  bbox,
}: {
  waterBodyId: string;
  date: string;
  bbox: number[] | undefined;
}) {
  const layers = useUi((s) => s.layers);
  // Chips only cover the body's bbox; without this Leaflet asks for the whole
  // viewport grid and every tile off the chip comes back 404.
  const bounds = bbox
    ? new L.LatLngBounds([bbox[1], bbox[0]], [bbox[3], bbox[2]])
    : undefined;
  return (
    <>
      {RASTER_LAYERS.filter((l) => layers[l.key]).map((l) => (
        <TileLayer
          key={`${l.key}-${date}`}
          url={api.tiles.template(l.key, waterBodyId, date)}
          bounds={bounds}
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
  const focusAlertId = useUi((s) => s.focusAlertId);
  const focusAlert = useUi((s) => s.focusAlert);
  const body = useWaterBody(waterBodyId);
  const alerts = useAlertsGeo({ status: "active" });
  const [layersOpen, setLayersOpen] = useState(false);
  // Shared with the top bar's place autocomplete (WaterBodyBar) so either
  // search box flies the map and shows the same discovered water bodies.
  const searchResult = useUi((s) => s.search);
  const setSearchResult = useUi((s) => s.setSearchResult);

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
        <FitToBody bbox={body.data?.bbox} enabled={!focusAlertId} />
        <LiveImageryLayer />
        {boundary && (
          <GeoJSON
            key={`b-${body.data?.id}`}
            data={boundary}
            // Outline only -- the satellite/basemap underneath is the point,
            // not a solid tint covering the whole water body.
            style={{ color: "#0284c7", weight: 2.5, fill: false, fillOpacity: 0 }}
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
              fillOpacity: f?.properties?.id === zoneId ? 0.05 : 0,
            })}
            onEachFeature={(f, layer) => {
              const p = f.properties as { id: string; name: string; baseline_status: string; open_alert_id: string | null };
              layer.bindTooltip(`${p.name} · baseline ${p.baseline_status}`, { sticky: true });
              layer.on("click", () => selectZone(p.id === zoneId ? null : p.id));
            }}
          />
        )}
        {waterBodyId && date && (
          <RasterLayers waterBodyId={waterBodyId} date={date} bbox={body.data?.bbox} />
        )}
        {searchResult && <MapSearchOverlay {...searchResult} />}
        {showAlerts && alerts.data && (
          <GeoJSON
            key={`a-${alerts.data.features.length}`}
            data={alerts.data as unknown as GJFeatureCollection}
            style={(f) => {
              const sev = (f?.properties?.severity ?? "low") as keyof typeof severityColor;
              // Outline only -- this layer spans every water body with an
              // active alert on the whole map, so a solid fill (low severity
              // is blue) reads as neighboring lakes being masked solid blue.
              return { color: severityColor[sev], weight: 2, fill: false, fillOpacity: 0 };
            }}
            onEachFeature={(f, layer) => {
              const p = f.properties as {
                alert_id: string;
                water_body_id: string;
                water_body_name: string;
                zone_id: string;
                zone_name: string;
                severity: string;
                priority_score: number;
                observed_on: string;
              };
              layer.bindTooltip(
                `<b>${p.water_body_name}</b> · ${p.zone_name}<br/>${p.severity.toUpperCase()} · priority ${Math.round(p.priority_score)} · ${p.observed_on}`,
                { sticky: true },
              );
              layer.on("click", () => {
                // Focus *and* open: picking an alert off the map used to only
                // open the sheet, leaving the viewport on the whole lake with
                // nothing but a hairline outline marking the flagged area.
                focusAlert({
                  alertId: p.alert_id,
                  waterBodyId: p.water_body_id,
                  date: p.observed_on,
                  zoneId: p.zone_id,
                });
                openAlert(p.alert_id);
              });
              if (p.severity === "high" && layer instanceof L.Path) layer.setStyle({ weight: 3 });
            }}
          />
        )}
        {focusAlertId && (
          <AlertFocusLayer alertId={focusAlertId} onSelect={() => openAlert(focusAlertId)} />
        )}
      </MapContainer>
      {focusAlertId && <FocusBanner alertId={focusAlertId} onClear={() => focusAlert(null)} />}
      <MapSearchWidget
        onResult={setSearchResult}
        onClear={() => setSearchResult(null)}
        hasResult={!!searchResult}
      />
      {/* Layers stay behind a button: the map is the point, not the controls. */}
      <div className="pointer-events-none absolute inset-y-3 right-3 z-[1000] flex flex-col items-end gap-2">
        <button
          onClick={() => setLayersOpen((v) => !v)}
          className="pointer-events-auto flex shrink-0 items-center gap-1.5 rounded-md border bg-card/95 px-2.5 py-1.5 text-xs font-medium shadow hover:bg-accent"
          title={layersOpen ? "Hide layer controls" : "Show layer controls"}
          aria-expanded={layersOpen}
        >
          {layersOpen ? <X className="h-3.5 w-3.5" /> : <Layers className="h-3.5 w-3.5" />}
          Layers
        </button>
        {layersOpen && <LayerToggles disabled={!waterBodyId || !date} />}
      </div>
      {alerts.data && (
        <div className="absolute bottom-3 left-3 z-[1000] max-w-md">
          <Disclaimer text={alerts.data.disclaimer} className="bg-amber-50/95 shadow" />
        </div>
      )}
    </div>
  );
}
