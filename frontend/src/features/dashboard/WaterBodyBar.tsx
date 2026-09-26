import { useEffect, useMemo, useRef, useState } from "react";
import { MapPin, Search } from "lucide-react";
import {
  useAddToWishlist,
  useDiscoverAtPoint,
  useImportDynamic,
  usePlaceSuggestions,
  useWaterBodies,
} from "@/api/hooks";
import type { DiscoveredWaterBodyOut, PlaceSuggestion } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { ErrorState } from "@/components/States";
import { Skeleton } from "@/components/ui/skeleton";
import { PipelineRunner } from "@/features/jobs/PipelineRunner";
import { STATUS_DOT, fmtDateShort, fmtKm2, severityBg, statusLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useUi } from "@/store/ui";

const AUTOCOMPLETE_DEBOUNCE_MS = 300;
const AUTOCOMPLETE_MIN_CHARS = 3;
const AUTO_DISCOVER_RADIUS_KM = 15;

/** A discovered-but-not-yet-tracked lake, appended to the strip after a place
 * search (S14). One click imports it, wishlists it and opens it -- "search,
 * then immediately inspect" in a single action. */
function DiscoveredChip({
  candidate,
  district,
}: {
  candidate: DiscoveredWaterBodyOut;
  district: string | null;
}) {
  const importDynamic = useImportDynamic();
  const addToWishlist = useAddToWishlist();
  const select = useUi((s) => s.selectWaterBody);
  const isPlaceholder = candidate.osm_id.startsWith("placeholder:");
  const busy = importDynamic.isPending || addToWishlist.isPending;

  async function inspect() {
    if (isPlaceholder || busy) return;
    if (candidate.already_registered_id) {
      select(candidate.already_registered_id);
      return;
    }
    const imported = await importDynamic.mutateAsync({
      osm_id: candidate.osm_id,
      name: candidate.name,
      kind: candidate.kind,
      geometry: candidate.geometry,
      district: district ?? "Unknown",
      source: "osm",
    });
    addToWishlist.mutate({ water_body_id: imported.water_body_id, custom_name: candidate.name });
    select(imported.water_body_id);
  }

  return (
    <button
      onClick={() => void inspect()}
      disabled={isPlaceholder || busy}
      title={
        isPlaceholder
          ? "OpenStreetMap has no water body mapped here"
          : `${candidate.name} — newly discovered, click to track & inspect`
      }
      className={cn(
        "flex shrink-0 items-center gap-2 rounded-md border border-dashed px-2.5 py-1 text-left transition-colors disabled:cursor-not-allowed",
        isPlaceholder ? "opacity-50" : "border-amber-400 bg-amber-50 hover:bg-amber-100",
      )}
    >
      <MapPin className="h-3.5 w-3.5 shrink-0 text-amber-600" />
      <span className="leading-tight">
        <span className="block max-w-[10rem] truncate whitespace-nowrap text-xs font-medium">
          {candidate.name}
        </span>
        <span className="block whitespace-nowrap text-[10px] text-muted-foreground">
          {busy ? "Adding…" : isPlaceholder ? "Not mapped yet" : `${fmtKm2(candidate.area_km2)} · new`}
        </span>
      </span>
    </button>
  );
}

/** Google-Maps-style "anywhere in India" search: live Nominatim suggestions as
 * the user types, and picking one flies the map there and auto-discovers
 * every water body nearby (S14/critical-fix). The same text also still
 * narrows the strip of already-tracked bodies below, unchanged. */
function PlaceSearchBox({ q, setQ }: { q: string; setQ: (q: string) => void }) {
  const [debouncedQ, setDebouncedQ] = useState("");
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const setSearchResult = useUi((s) => s.setSearchResult);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQ(q.trim()), AUTOCOMPLETE_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [q]);

  const suggestQuery = debouncedQ.length >= AUTOCOMPLETE_MIN_CHARS ? debouncedQ : null;
  const suggestions = usePlaceSuggestions(suggestQuery);
  const discoverAtPoint = useDiscoverAtPoint();

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  function pick(s: PlaceSuggestion) {
    setOpen(false);
    discoverAtPoint.mutate(
      { latitude: s.lat, longitude: s.lon, radius_km: AUTO_DISCOVER_RADIUS_KM },
      {
        onSuccess: (res) =>
          setSearchResult({
            centre: res.centre,
            radiusKm: res.radius_km,
            district: s.district,
            items: res.items,
          }),
      },
    );
  }

  return (
    <div className="relative w-56 shrink-0" ref={boxRef}>
      <label className="flex items-center gap-2 rounded-md border bg-card px-2 py-1.5 text-sm">
        <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <input
          className="w-full bg-transparent outline-none placeholder:text-muted-foreground"
          placeholder="Search anywhere in India…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
        />
      </label>
      {open && suggestQuery && (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 max-h-64 overflow-y-auto rounded-md border bg-card text-xs shadow-lg">
          {suggestions.isFetching && <div className="px-3 py-2 text-muted-foreground">Searching…</div>}
          {!suggestions.isFetching && (suggestions.data?.items.length ?? 0) === 0 && (
            <div className="px-3 py-2 text-muted-foreground">No places found in India.</div>
          )}
          {suggestions.data?.items.map((s, i) => (
            <button
              key={`${s.lat}-${s.lon}-${i}`}
              onClick={() => pick(s)}
              className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-accent"
            >
              <MapPin className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate">{s.display_name}</span>
            </button>
          ))}
          {discoverAtPoint.isPending && (
            <div className="border-t px-3 py-2 text-muted-foreground">Discovering water bodies…</div>
          )}
          {discoverAtPoint.error && (
            <div className="border-t px-3 py-2 text-destructive">Could not scan that location.</div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * The water-body context bar across the top: search, one chip per body, and
 * the pipeline runner for whichever is selected. Bodies with open alerts sort
 * first, so the queue that matters is reachable without scrolling.
 */
export function WaterBodyBar() {
  const [q, setQ] = useState("");
  const [district, setDistrict] = useState("");
  const { data, isLoading, error, refetch } = useWaterBodies();
  const selected = useUi((s) => s.waterBodyId);
  const select = useUi((s) => s.selectWaterBody);
  const search = useUi((s) => s.search);
  const stripRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLButtonElement>(null);

  // The registry spans several districts; the strip stays usable by narrowing.
  const districts = useMemo(
    () => [...new Set((data?.items ?? []).map((r) => r.district))].sort(),
    [data],
  );

  const items = useMemo(() => {
    const rows = data?.items ?? [];
    const needle = q.trim().toLowerCase();
    const filtered = rows.filter(
      (r) =>
        (!district || r.district === district) &&
        (!needle || `${r.name} ${r.district}`.toLowerCase().includes(needle)),
    );
    return [...filtered].sort(
      (a, b) =>
        Number(b.open_alerts > 0) - Number(a.open_alerts > 0) ||
        a.tier - b.tier ||
        a.name.localeCompare(b.name),
    );
  }, [data, q, district]);

  // The strip scrolls; keep the selected body visible when it changes.
  useEffect(() => {
    const el = activeRef.current;
    const strip = stripRef.current;
    if (!el || !strip) return;
    // Rect maths, not offsetLeft: the strip is not the element's offsetParent.
    const er = el.getBoundingClientRect();
    const sr = strip.getBoundingClientRect();
    const left =
      strip.scrollLeft + (er.left - sr.left) - sr.width / 2 + er.width / 2;
    strip.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
  }, [selected, items.length]);

  // Newly discovered lakes from a place search that aren't already tracked --
  // appended to the strip so the user can click straight into one.
  const discovered = (search?.items ?? []).filter((c) => !c.already_registered_id);

  return (
    <div className="flex min-w-0 items-center gap-3 border-b bg-card px-3 py-2">
      <PlaceSearchBox q={q} setQ={setQ} />

      {districts.length > 1 && (
        <select
          className="shrink-0 rounded-md border bg-card px-1.5 py-1.5 text-xs"
          value={district}
          onChange={(e) => setDistrict(e.target.value)}
          aria-label="District"
        >
          <option value="">All districts</option>
          {districts.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      )}

      <div className="relative min-w-0 flex-1">
        <div ref={stripRef} className="flex items-center gap-2 overflow-x-auto py-0.5">
          {isLoading &&
            [0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-10 w-44 shrink-0 rounded-md" />
            ))}
          {error && <ErrorState error={error} onRetry={() => void refetch()} />}
          {data && items.length === 0 && (
            <span className="text-xs text-muted-foreground">
              No water bodies match — seed the registry or clear the search.
            </span>
          )}
          {items.map((wb) => (
            <button
              key={wb.id}
              ref={selected === wb.id ? activeRef : undefined}
              onClick={() => select(wb.id)}
              title={`${wb.name} · ${wb.district} · ${fmtKm2(wb.area_km2)} · tier ${wb.tier} · ${statusLabel[wb.status]}`}
              className={cn(
                "flex shrink-0 items-center gap-2 rounded-md border px-2.5 py-1 text-left transition-colors",
                selected === wb.id ? "border-primary bg-accent" : "hover:bg-accent/60",
              )}
            >
              <span className={cn("h-2 w-2 shrink-0 rounded-full", STATUS_DOT[wb.status])} />
              <span className="leading-tight">
                <span className="block whitespace-nowrap text-xs font-medium">{wb.name}</span>
                <span className="block whitespace-nowrap text-[10px] text-muted-foreground">
                  {wb.district} · {fmtKm2(wb.area_km2)}
                  {wb.latest_observation && ` · ${fmtDateShort(wb.latest_observation.observed_on)}`}
                </span>
              </span>
              {wb.open_alerts > 0 && wb.max_open_severity && (
                <Badge className={cn("shrink-0", severityBg[wb.max_open_severity])} title={`${wb.open_alerts} open`}>
                  {wb.open_alerts}
                </Badge>
              )}
            </button>
          ))}
          {discovered.map((c) => (
            <DiscoveredChip key={c.osm_id} candidate={c} district={search?.district ?? null} />
          ))}
        </div>
        {/* Hints that the strip keeps going; the registry is longer than the bar. */}
        <div className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-card to-transparent" />
      </div>

      {/* Pinned to the far right corner of the same row -- never wraps to a
       * second line; the strip above shrinks to make room instead. */}
      <div className="ml-auto shrink-0 border-l pl-3">
        <PipelineRunner />
      </div>
    </div>
  );
}
