/**
 * Quick-look inspection drawer for a single water body — opened from the
 * wishlist, recent history, or a pin on the map (S14). Deliberately reads
 * `ui.detailsWaterBodyId`, not `ui.waterBodyId`: looking a lake up here must
 * never disturb whatever the main dashboard has selected.
 *
 * The timelapse animates the pipeline's own indicator/water-mask chips
 * (instant, already computed) rather than asking Earth Engine for a fresh
 * render on every 500 ms frame — a live GEE round trip runs 1-20+ s on this
 * project's own numbers, which would make "Play" stutter rather than animate.
 * A separate "Live now" toggle still offers genuine Earth-Engine true/false
 * colour imagery for the current pass, reusing the existing `LiveImageryLayer`.
 */
import { useEffect, useMemo, useState } from "react";
import { addMonths } from "date-fns";
import L from "leaflet";
import { MapContainer, TileLayer, useMap } from "react-leaflet";
import { Maximize2, Pause, Play, Star, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import {
  useAddToWishlist,
  useIndicators,
  useObservations,
  useRemoveFromWishlist,
  useWaterBody,
  useWishlist,
} from "@/api/hooks";
import type { RasterLayer, ZoneIndicator } from "@/api/types";
import { Disclaimer } from "@/components/Disclaimer";
import { ErrorState, PanelSkeleton } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { FetchSatelliteButton } from "@/features/dashboard/FetchSatelliteButton";
import { RASTER_LAYERS, fmtDate, fmtKm2, fmtSigned, statusLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

const RANGE_OPTIONS = [
  { key: "1m", label: "Past 1 month", months: 1 },
  { key: "3m", label: "Past 3 months", months: 3 },
  { key: "6m", label: "Past 6 months", months: 6 },
  { key: "1y", label: "Past 1 year", months: 12 },
] as const;
type RangeKey = (typeof RANGE_OPTIONS)[number]["key"];

const ANIMATION_FRAME_MS = 500;

/** Worst (largest |z|) reading for one indicator key across every zone. */
function worstReading(
  zones: { indicators: ZoneIndicator[] }[] | undefined,
  key: string,
): ZoneIndicator | null {
  let best: ZoneIndicator | null = null;
  for (const zone of zones ?? []) {
    for (const r of zone.indicators) {
      if (r.key !== key) continue;
      if (!best || Math.abs(r.z_score ?? 0) > Math.abs(best.z_score ?? 0)) best = r;
    }
  }
  return best;
}

function readingTone(r: ZoneIndicator | null): "neutral" | "good" | "watch" | "alert" {
  if (!r || r.baseline_status !== "usable" || r.z_score === null || r.z_score === undefined) {
    return "neutral";
  }
  const az = Math.abs(r.z_score);
  return az > 5 ? "alert" : az > 3 ? "watch" : "good";
}

const TONE_BADGE: Record<ReturnType<typeof readingTone>, string> = {
  neutral: "bg-muted text-muted-foreground",
  good: "bg-emerald-100 text-emerald-800",
  watch: "bg-amber-100 text-amber-800",
  alert: "bg-red-100 text-red-800",
};
const TONE_WORD: Record<ReturnType<typeof readingTone>, string> = {
  neutral: "Building baseline",
  good: "Normal",
  watch: "Watch",
  alert: "Elevated",
};

type IndicatorKind = "ndti_turbidity" | "ndci_chlorophyll" | "fai_algal";

/** Domain threshold on the raw indicator value, used only while there is no
 * baseline yet to z-score against -- the current scene's own reading is
 * already computed regardless, so there is no reason to hide it. */
function rawValueReading(key: IndicatorKind, value: number): { tone: "good" | "watch"; word: string } {
  switch (key) {
    case "ndti_turbidity":
      return value > 0.1 ? { tone: "watch", word: "Turbid" } : { tone: "good", word: "Clear" };
    case "ndci_chlorophyll":
      return value > 0.05 ? { tone: "watch", word: "High Algae" } : { tone: "good", word: "Normal" };
    case "fai_algal":
      return value > 0 ? { tone: "watch", word: "Elevated" } : { tone: "good", word: "Normal" };
  }
}

function IndicatorBadge({
  label,
  indicatorKey,
  reading,
}: {
  label: string;
  indicatorKey: IndicatorKind;
  reading: ZoneIndicator | null;
}) {
  const baselineTone = readingTone(reading);
  const hasValue = reading?.value !== null && reading?.value !== undefined;
  // Only falls back to the raw-value read when there is genuinely no z-scored
  // baseline yet ("neutral") -- a usable baseline always wins, even a calm one.
  const raw = baselineTone === "neutral" && hasValue ? rawValueReading(indicatorKey, reading!.value!) : null;
  const badgeClass = raw ? TONE_BADGE[raw.tone] : TONE_BADGE[baselineTone];
  const word = raw ? raw.word : hasValue || baselineTone !== "neutral" ? TONE_WORD[baselineTone] : "No data yet";
  return (
    <Badge className={cn("normal-case", badgeClass)}>
      {label}: {word}
      {hasValue && ` (${reading!.value!.toFixed(2)})`}
      {baselineTone !== "neutral" && reading?.z_score != null && ` · ${fmtSigned(reading.z_score)}σ`}
    </Badge>
  );
}

function FitBbox({ bbox }: { bbox: number[] }) {
  const map = useMap();
  useEffect(() => {
    const [minx, miny, maxx, maxy] = bbox;
    map.fitBounds(
      [
        [miny, minx],
        [maxy, maxx],
      ],
      { padding: [4, 4], animate: false },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bbox.join(","), map]);
  return null;
}

/** Full-screen click-to-zoom view of one raster chip: same tile, a real
 * (draggable, zoomable) map, the pass timestamp and the lake name. */
function RasterLightbox({
  waterBodyId,
  bbox,
  layer,
  date,
  lakeName,
  onClose,
}: {
  waterBodyId: string;
  bbox: number[];
  layer: RasterLayer;
  date: string;
  lakeName: string;
  onClose: () => void;
}) {
  const bounds = useMemo(() => new L.LatLngBounds([bbox[1], bbox[0]], [bbox[3], bbox[2]]), [bbox]);
  const layerLabel = RASTER_LAYERS.find((l) => l.key === layer)?.label ?? layer;
  return (
    <div
      className="fixed inset-0 z-[3000] flex items-center justify-center bg-black/85 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="flex h-full max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-lg bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-2 border-b px-4 py-2.5">
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">{lakeName}</div>
            <div className="text-xs text-muted-foreground">
              {layerLabel} · {fmtDate(date)} UTC
            </div>
          </div>
          <button
            onClick={onClose}
            className="shrink-0 rounded-md p-1 text-muted-foreground hover:bg-muted"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="relative flex-1 bg-muted">
          <MapContainer
            center={[(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2]}
            zoom={13}
            className="h-full w-full"
            zoomControl
            preferCanvas
          >
            <FitBbox bbox={bbox} />
            <TileLayer
              key={`${layer}-${date}`}
              url={api.tiles.template(layer, waterBodyId, date)}
              bounds={bounds}
              opacity={layer === "watermask" ? 0.6 : 0.95}
            />
          </MapContainer>
        </div>
      </div>
    </div>
  );
}

/** Non-interactive map locked to a bbox, rendering one raster chip on one
 * date — the building block for both the animation frame and the comparison.
 * Click it (or the expand button) for a full-screen, zoomable view. */
function MiniRasterMap({
  waterBodyId,
  bbox,
  layer,
  date,
  lakeName,
}: {
  waterBodyId: string;
  bbox: number[];
  layer: RasterLayer;
  date: string | null;
  lakeName: string;
}) {
  const [zoomed, setZoomed] = useState(false);
  const bounds = useMemo(() => new L.LatLngBounds([bbox[1], bbox[0]], [bbox[3], bbox[2]]), [bbox]);
  return (
    <>
      <button
        type="button"
        className="group relative block h-56 w-full overflow-hidden rounded-lg border bg-muted md:h-64"
        onClick={() => date && setZoomed(true)}
        disabled={!date}
        aria-label={date ? `Enlarge ${layer} chip for ${date}` : "No pass in range"}
      >
        <MapContainer
          center={[(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2]}
          zoom={12}
          className="h-full w-full"
          zoomControl={false}
          dragging={false}
          scrollWheelZoom={false}
          doubleClickZoom={false}
          touchZoom={false}
          boxZoom={false}
          keyboard={false}
          attributionControl={false}
          preferCanvas
        >
          <FitBbox bbox={bbox} />
          {date && (
            <TileLayer
              key={`${layer}-${date}`}
              url={api.tiles.template(layer, waterBodyId, date)}
              bounds={bounds}
              opacity={layer === "watermask" ? 0.6 : 0.9}
            />
          )}
        </MapContainer>
        {!date && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-[11px] text-muted-foreground">
            No pass in range
          </div>
        )}
        {date && (
          <span className="pointer-events-none absolute right-2 top-2 flex items-center gap-1 rounded-md bg-black/60 px-2 py-1 text-[11px] text-white opacity-0 transition-opacity group-hover:opacity-100">
            <Maximize2 className="h-3 w-3" /> Enlarge
          </span>
        )}
      </button>
      {zoomed && date && (
        <RasterLightbox
          waterBodyId={waterBodyId}
          bbox={bbox}
          layer={layer}
          date={date}
          lakeName={lakeName}
          onClose={() => setZoomed(false)}
        />
      )}
    </>
  );
}

function LayerPicker({ value, onChange }: { value: RasterLayer; onChange: (l: RasterLayer) => void }) {
  return (
    <select
      className="h-7 rounded border bg-background px-1.5 text-xs"
      value={value}
      onChange={(e) => onChange(e.target.value as RasterLayer)}
      aria-label="Raster layer"
    >
      {RASTER_LAYERS.map((l) => (
        <option key={l.key} value={l.key}>
          {l.label}
        </option>
      ))}
    </select>
  );
}

/** Custom date-range selector + play/pause animation over the pipeline's own
 * chips, cycling one frame every 500 ms while playing (per the spec). */
function TimelapsePlayer({
  waterBodyId,
  bbox,
  lakeName,
}: {
  waterBodyId: string;
  bbox: number[];
  lakeName: string;
}) {
  const observations = useObservations(waterBodyId);
  const [range, setRange] = useState<RangeKey>("3m");
  const [layer, setLayer] = useState<RasterLayer>("ndti_turbidity");
  const [playing, setPlaying] = useState(false);
  const [frame, setFrame] = useState(0);

  const months = RANGE_OPTIONS.find((r) => r.key === range)!.months;
  const usable = useMemo(() => {
    const cutoff = addMonths(new Date(), -months);
    return [...(observations.data?.items ?? [])]
      .filter((o) => o.usable && new Date(o.observed_on) >= cutoff)
      .reverse(); // oldest -> newest, so playback runs forward in time
  }, [observations.data, months]);

  // Range/layer change or the list shrinking must not leave `frame` pointing
  // past the end (an empty player, not a crash, if a range has no passes).
  useEffect(() => {
    setFrame((f) => Math.min(f, Math.max(0, usable.length - 1)));
  }, [usable.length]);

  useEffect(() => {
    if (!playing || usable.length < 2) return;
    const id = window.setInterval(() => {
      setFrame((f) => (f + 1) % usable.length);
    }, ANIMATION_FRAME_MS);
    return () => window.clearInterval(id);
  }, [playing, usable.length]);

  const current = usable[frame] ?? null;

  return (
    <section>
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Historical timelapse
        </h3>
        <select
          className="h-7 rounded border bg-background px-1.5 text-xs"
          value={range}
          onChange={(e) => {
            setRange(e.target.value as RangeKey);
            setFrame(0);
            setPlaying(false);
          }}
          aria-label="Date range"
        >
          {RANGE_OPTIONS.map((r) => (
            <option key={r.key} value={r.key}>
              {r.label}
            </option>
          ))}
        </select>
        <LayerPicker value={layer} onChange={setLayer} />
        <Button
          size="sm"
          variant="outline"
          disabled={usable.length < 2}
          onClick={() => setPlaying((p) => !p)}
          className="ml-auto"
        >
          {playing ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
          {playing ? "Pause" : "Play"}
        </Button>
      </div>

      {observations.isLoading && <PanelSkeleton rows={3} />}
      {observations.error && <ErrorState error={observations.error} onRetry={() => void observations.refetch()} />}
      {observations.data && (
        <>
          <MiniRasterMap
            waterBodyId={waterBodyId}
            bbox={bbox}
            layer={layer}
            date={current?.observed_on ?? null}
            lakeName={lakeName}
          />
          <div className="mt-1.5 flex items-center gap-2">
            <input
              type="range"
              className="w-full"
              min={0}
              max={Math.max(0, usable.length - 1)}
              value={frame}
              disabled={usable.length < 2}
              onChange={(e) => {
                setPlaying(false);
                setFrame(Number(e.target.value));
              }}
              aria-label="Scrub through passes"
            />
            <span className="w-24 shrink-0 text-right text-[11px] text-muted-foreground">
              {current ? fmtDate(current.observed_on) : `0 / 0`}
            </span>
          </div>
          {!usable.length && (
            <p className="mt-1 text-xs text-muted-foreground">
              {observations.data.items.length === 0
                ? 'No satellite passes processed yet for this lake — use "Fetch satellite data" above to start.'
                : "No usable pass in this window — widen the date range."}
            </p>
          )}
        </>
      )}
    </section>
  );
}

/** Date A vs Date B, side by side, same layer — the drawdown-vs-monsoon view. */
function ComparisonSlider({
  waterBodyId,
  bbox,
  lakeName,
}: {
  waterBodyId: string;
  bbox: number[];
  lakeName: string;
}) {
  const observations = useObservations(waterBodyId);
  const [layer, setLayer] = useState<RasterLayer>("mndwi_extent");
  const usable = useMemo(
    () => [...(observations.data?.items ?? [])].filter((o) => o.usable).reverse(),
    [observations.data],
  );
  const [dateA, setDateA] = useState<string | null>(null);
  const [dateB, setDateB] = useState<string | null>(null);

  useEffect(() => {
    if (!usable.length) return;
    setDateA((d) => d ?? usable[0].observed_on);
    setDateB((d) => d ?? usable[usable.length - 1].observed_on);
  }, [usable]);

  if (!usable.length) {
    return (
      <section>
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Side-by-side comparison
        </h3>
        <p className="text-xs text-muted-foreground">
          No usable passes yet — use &quot;Fetch satellite data&quot; above to get a before/after view.
        </p>
      </section>
    );
  }

  return (
    <section>
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Side-by-side comparison
        </h3>
        <LayerPicker value={layer} onChange={setLayer} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        {(
          [
            ["A", dateA, setDateA],
            ["B", dateB, setDateB],
          ] as const
        ).map(([label, date, setDate]) => (
          <div key={label} className="space-y-1">
            <div className="flex items-center gap-1.5">
              <Badge className="bg-muted text-muted-foreground">{label}</Badge>
              <select
                className="h-7 min-w-0 flex-1 rounded border bg-background px-1.5 text-xs"
                value={date ?? ""}
                onChange={(e) => setDate(e.target.value)}
                aria-label={`Date ${label}`}
              >
                {usable.map((o) => (
                  <option key={o.scene_id} value={o.observed_on}>
                    {fmtDate(o.observed_on)}
                  </option>
                ))}
              </select>
            </div>
            <MiniRasterMap waterBodyId={waterBodyId} bbox={bbox} layer={layer} date={date} lakeName={lakeName} />
          </div>
        ))}
      </div>
    </section>
  );
}

function Header({ waterBodyId }: { waterBodyId: string }) {
  const navigate = useNavigate();
  const wb = useWaterBody(waterBodyId);
  const observations = useObservations(waterBodyId);
  const indicators = useIndicators(waterBodyId);
  const wishlist = useWishlist();
  const selectWaterBody = useUi((s) => s.selectWaterBody);
  const openDetails = useUi((s) => s.openDetails);
  const addToWishlist = useAddToWishlist();
  const removeFromWishlist = useRemoveFromWishlist();

  const item = wishlist.data?.items.find((i) => i.water_body_id === waterBodyId) ?? null;
  const latestObservation = observations.data?.items.find((o) => o.observed_on === indicators.data?.observed_on);
  const capacityPct =
    wb.data && latestObservation?.water_extent_km2 != null
      ? (latestObservation.water_extent_km2 / wb.data.area_km2) * 100
      : null;

  return (
    <div className="space-y-2 border-b p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-base font-semibold">{item?.custom_name ?? wb.data?.name}</div>
          {item && item.custom_name !== wb.data?.name && (
            <div className="truncate text-xs text-muted-foreground">Registry name: {wb.data?.name}</div>
          )}
          <div className="truncate text-xs text-muted-foreground">
            {wb.data ? `${wb.data.district} · ${fmtKm2(wb.data.area_km2)} · tier ${wb.data.tier}` : ""}
            {wb.data && ` · ${statusLabel[wb.data.status] ?? wb.data.status}`}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {wb.data && <FetchSatelliteButton waterBodyId={waterBodyId} lakeName={wb.data.name} />}
          <Button
            size="icon"
            variant="outline"
            title={item ? "Remove from wishlist" : "Save to wishlist"}
            disabled={addToWishlist.isPending || removeFromWishlist.isPending}
            onClick={() =>
              item
                ? removeFromWishlist.mutate(item.id)
                : addToWishlist.mutate({ water_body_id: waterBodyId, custom_name: null })
            }
          >
            <Star className={cn("h-4 w-4", item && "fill-amber-500 text-amber-500")} />
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        <IndicatorBadge
          label="Turbidity"
          indicatorKey="ndti_turbidity"
          reading={worstReading(indicators.data?.zones, "ndti_turbidity")}
        />
        <IndicatorBadge
          label="Chlorophyll"
          indicatorKey="ndci_chlorophyll"
          reading={worstReading(indicators.data?.zones, "ndci_chlorophyll")}
        />
        <IndicatorBadge
          label="Algae"
          indicatorKey="fai_algal"
          reading={worstReading(indicators.data?.zones, "fai_algal")}
        />
        <Badge className="bg-sky-100 text-sky-800 normal-case">
          Surface extent:{" "}
          {latestObservation?.water_extent_km2 != null
            ? `${latestObservation.water_extent_km2.toFixed(2)} km²`
            : "—"}
          {capacityPct !== null && ` (${capacityPct.toFixed(0)}% of registered area)`}
        </Badge>
      </div>

      <Button
        size="sm"
        variant="secondary"
        onClick={() => {
          selectWaterBody(waterBodyId);
          openDetails(null);
          navigate("/");
        }}
      >
        View full dashboard
      </Button>
    </div>
  );
}

function Body({ waterBodyId }: { waterBodyId: string }) {
  const wb = useWaterBody(waterBodyId);
  const indicators = useIndicators(waterBodyId);
  if (wb.isLoading) return <PanelSkeleton rows={6} />;
  if (wb.error) return <ErrorState error={wb.error} onRetry={() => void wb.refetch()} />;
  if (!wb.data) return null;
  return (
    <div className="flex flex-col">
      <Header waterBodyId={waterBodyId} />
      <div className="space-y-4 p-4">
        <TimelapsePlayer waterBodyId={waterBodyId} bbox={wb.data.bbox} lakeName={wb.data.name} />
        <ComparisonSlider waterBodyId={waterBodyId} bbox={wb.data.bbox} lakeName={wb.data.name} />
        <Disclaimer text={indicators.data?.disclaimer} />
      </div>
    </div>
  );
}

/** Global quick-look drawer; opens whenever `ui.detailsWaterBodyId` is set. */
export function LakeDetailsDrawer() {
  const id = useUi((s) => s.detailsWaterBodyId);
  const openDetails = useUi((s) => s.openDetails);
  const wb = useWaterBody(id);
  return (
    <Sheet
      open={!!id}
      onOpenChange={(o) => !o && openDetails(null)}
      title={wb.data?.name ?? "Water body"}
      description={wb.data ? `${wb.data.district} · ${wb.data.kind}` : (id ?? "")}
      className="max-w-3xl"
    >
      {id && <Body waterBodyId={id} />}
    </Sheet>
  );
}
