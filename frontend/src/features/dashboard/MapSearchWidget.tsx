/**
 * Floating "find a lake by place name" widget on the map (S14). Resolves a
 * city or water-body name, flies to it, draws the search radius, and
 * highlights every OSM water polygon Overpass found inside it. Saving one
 * that is not yet in the registry imports it first (so the wishlist's FK to
 * `water_bodies` is always satisfiable) and then wishlists the result.
 */
import { useEffect, useState } from "react";
import type { LatLngExpression } from "leaflet";
import { Circle, Polygon, Popup, useMap } from "react-leaflet";
import { Loader2, Search, Star, X } from "lucide-react";
import { useAddToWishlist, useImportDynamic, useSearchAndDiscover } from "@/api/hooks";
import { ApiError } from "@/api/http";
import type { DiscoveredWaterBodyOut } from "@/api/types";
import { Button } from "@/components/ui/button";
import { fmtKm2 } from "@/lib/format";

const RADII_KM = [5, 15, 30, 50] as const;

/** A GeoJSON Polygon/MultiPolygon's outer ring(s), as Leaflet [lat, lon] rings.
 * Holes are dropped -- a click target for "is this the lake I meant", not a
 * precise boundary, so an island in a lake costs nothing here. */
function outerRings(geometry: DiscoveredWaterBodyOut["geometry"]): LatLngExpression[][] {
  const g = geometry as { type: string; coordinates: unknown };
  const toLatLng = (ring: number[][]): LatLngExpression[] =>
    ring.map(([lon, lat]) => [lat, lon] as LatLngExpression);
  if (g.type === "Polygon") return [toLatLng((g.coordinates as number[][][])[0])];
  if (g.type === "MultiPolygon") return (g.coordinates as number[][][][]).map((poly) => toLatLng(poly[0]));
  return [];
}

function FlyTo({ lon, lat }: { lon: number; lat: number }) {
  const map = useMap();
  useEffect(() => {
    map.flyTo([lat, lon], 12, { duration: 1 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lon, lat]);
  return null;
}

function CandidatePopup({
  candidate,
  district,
}: {
  candidate: DiscoveredWaterBodyOut;
  district: string | null;
}) {
  const addToWishlist = useAddToWishlist();
  const importDynamic = useImportDynamic();
  const busy = addToWishlist.isPending || importDynamic.isPending;
  const done = addToWishlist.isSuccess || importDynamic.isSuccess;
  // OSM has nothing mapped here (or Overpass was unreachable); the backend
  // sends a placeholder box just so the location can still be inspected on
  // the map -- it isn't a real water body, so it can't be saved as one.
  const isPlaceholder = candidate.osm_id.startsWith("placeholder:");

  async function save() {
    const label = window.prompt("Label for your wishlist (optional)", candidate.name) ?? candidate.name;
    if (candidate.already_registered_id) {
      addToWishlist.mutate({ water_body_id: candidate.already_registered_id, custom_name: label });
      return;
    }
    const chosenDistrict = window.prompt(
      "District (required to register this water body)",
      district ?? "",
    );
    if (!chosenDistrict) return; // cancelled — nothing imported, nothing saved
    const imported = await importDynamic.mutateAsync({
      osm_id: candidate.osm_id,
      name: candidate.name,
      kind: candidate.kind,
      geometry: candidate.geometry,
      district: chosenDistrict,
      source: "osm",
    });
    addToWishlist.mutate({ water_body_id: imported.water_body_id, custom_name: label });
  }

  return (
    <div className="min-w-[12rem] space-y-1 text-xs">
      <div className="font-semibold">{candidate.name}</div>
      {isPlaceholder ? (
        <div className="text-muted-foreground">
          OpenStreetMap has no water body mapped here. Try a smaller radius, a
          different spelling, or the lake's own name.
        </div>
      ) : (
        <>
          <div className="text-muted-foreground">
            Area {fmtKm2(candidate.area_km2)} (Tier {candidate.suggested_tier}) · {candidate.kind.replace("_", " ")}
          </div>
          {candidate.already_registered_id ? (
            <div className="text-muted-foreground">Already tracked as {candidate.already_registered_id}</div>
          ) : (
            <div className="text-muted-foreground">Not yet registered — saving will import it first.</div>
          )}
          <Button
            size="sm"
            className="w-full bg-amber-500 text-white hover:bg-amber-600"
            disabled={busy || done}
            onClick={() => void save()}
          >
            <Star className="h-3.5 w-3.5" /> {done ? "Saved" : busy ? "Saving…" : "Save to wishlist"}
          </Button>
        </>
      )}
      {(addToWishlist.error || importDynamic.error) && (
        <div className="text-destructive">
          {(addToWishlist.error ?? importDynamic.error) instanceof Error
            ? (addToWishlist.error ?? importDynamic.error)!.message
            : "Could not save."}
        </div>
      )}
    </div>
  );
}

/** The discovered candidates + search-radius circle, drawn once a search has
 * results. A separate component so the map only re-renders this layer set,
 * not the whole widget, while typing in the search box. */
export function MapSearchOverlay({
  centre,
  radiusKm,
  district,
  items,
}: {
  centre: [number, number]; // [lon, lat]
  radiusKm: number;
  district: string | null;
  items: DiscoveredWaterBodyOut[];
}) {
  return (
    <>
      <FlyTo lon={centre[0]} lat={centre[1]} />
      <Circle
        center={[centre[1], centre[0]]}
        radius={radiusKm * 1000}
        pathOptions={{ color: "#0891b2", weight: 1.5, dashArray: "6 4", fill: false, fillOpacity: 0 }}
      />
      {/* Outline only, every case -- a discovered polygon must never mask the
       * base map or satellite imagery underneath with a solid tint. */}
      {items.map((c) =>
        outerRings(c.geometry).map((ring, i) => (
          <Polygon
            key={`${c.osm_id}-${i}`}
            positions={ring}
            pathOptions={
              c.osm_id.startsWith("placeholder:")
                ? { color: "#94a3b8", weight: 1.5, fill: false, fillOpacity: 0, dashArray: "2 6" }
                : c.already_registered_id
                  ? { color: "#64748b", weight: 1.5, fill: false, fillOpacity: 0, dashArray: "3 3" }
                  : { color: "#06b6d4", weight: 2, fill: false, fillOpacity: 0, dashArray: "4 3" }
            }
          >
            <Popup>
              <CandidatePopup candidate={c} district={district} />
            </Popup>
          </Polygon>
        )),
      )}
    </>
  );
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "Search failed.";
}

/**
 * The floating search box (top-left, mirrors the Layers control on the
 * right): a place/water-body name, a radius, and "Identify water bodies".
 * Renders nothing on the map itself — the caller mounts `overlay` (below)
 * inside the `<MapContainer>` once a search has a result.
 */
export function MapSearchWidget({
  onResult,
  onClear,
  hasResult,
}: {
  onResult: (result: {
    centre: [number, number];
    radiusKm: number;
    district: string | null;
    items: DiscoveredWaterBodyOut[];
  }) => void;
  onClear: () => void;
  hasResult: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [radiusKm, setRadiusKm] = useState<(typeof RADII_KM)[number]>(15);
  const search = useSearchAndDiscover();

  function identify() {
    const q = query.trim();
    if (!q) return;
    search.mutate(
      { query: q, radius_km: radiusKm },
      {
        onSuccess: (res) =>
          onResult({ centre: res.centre as [number, number], radiusKm: res.radius_km, district: res.district, items: res.items }),
      },
    );
  }

  return (
    <div className="pointer-events-none absolute inset-y-3 left-3 z-[1000] flex flex-col items-start gap-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="pointer-events-auto flex shrink-0 items-center gap-1.5 rounded-md border bg-card/95 px-2.5 py-1.5 text-xs font-medium shadow hover:bg-accent"
        title={open ? "Hide search" : "Search for water bodies"}
        aria-expanded={open}
      >
        {open ? <X className="h-3.5 w-3.5" /> : <Search className="h-3.5 w-3.5" />}
        Find water bodies
      </button>
      {open && (
        <div className="pointer-events-auto w-64 space-y-2 rounded-md border bg-card/95 p-2.5 text-xs shadow">
          <label className="flex items-center gap-2 rounded border bg-background px-2 py-1.5">
            <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <input
              className="w-full min-w-0 bg-transparent outline-none placeholder:text-muted-foreground"
              placeholder='"Nagpur" or "Ambazari Lake"'
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && identify()}
            />
          </label>
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground">Radius</span>
            <select
              className="h-7 flex-1 rounded border bg-background px-1.5"
              value={radiusKm}
              onChange={(e) => setRadiusKm(Number(e.target.value) as (typeof RADII_KM)[number])}
              aria-label="Search radius"
            >
              {RADII_KM.map((r) => (
                <option key={r} value={r}>
                  {r} km
                </option>
              ))}
            </select>
          </div>
          <Button size="sm" className="w-full" disabled={!query.trim() || search.isPending} onClick={identify}>
            {search.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
            Identify water bodies
          </Button>
          {hasResult && (
            <Button size="sm" variant="outline" className="w-full" onClick={onClear}>
              Clear results
            </Button>
          )}
          {search.error && <div className="text-destructive">{errorMessage(search.error)}</div>}
          {search.data && (
            <div className="text-muted-foreground">
              {search.data.total} water {search.data.total === 1 ? "body" : "bodies"} found near{" "}
              {search.data.resolved_place}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
