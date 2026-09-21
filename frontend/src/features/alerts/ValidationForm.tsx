import { useState } from "react";
import { format } from "date-fns";
import { CheckCircle2 } from "lucide-react";
import { api } from "@/api/client";
import { useSubmitValidation, useValidations } from "@/api/hooks";
import type { LabResults } from "@/api/types";
import { Button } from "@/components/ui/button";
import { ErrorState, PanelSkeleton } from "@/components/States";
import { fmtDate, fmtDateTime } from "@/lib/format";

const FIELDS: { key: keyof LabResults; label: string; unit: string; step?: string }[] = [
  { key: "turbidity_ntu", label: "Turbidity", unit: "NTU" },
  { key: "tss_mg_l", label: "TSS", unit: "mg/L" },
  { key: "chlorophyll_ug_l", label: "Chlorophyll-a", unit: "µg/L" },
  { key: "do_mg_l", label: "Dissolved oxygen", unit: "mg/L" },
  { key: "ph", label: "pH", unit: "", step: "0.1" },
  { key: "temperature_c", label: "Temperature", unit: "°C" },
  { key: "conductivity_us_cm", label: "Conductivity", unit: "µS/cm" },
];

/** Field / lab result submission against an alert (POST /validations). */
export function ValidationForm({ alertId }: { alertId: string }) {
  const [sampledOn, setSampledOn] = useState(format(new Date(), "yyyy-MM-dd"));
  const [by, setBy] = useState("");
  const [condition, setCondition] = useState("");
  const [notes, setNotes] = useState("");
  const [lab, setLab] = useState<Record<string, string>>({});
  const [photo, setPhoto] = useState<File | null>(null);
  const [photoState, setPhotoState] = useState<string | null>(null);
  const submit = useSubmitValidation();
  const existing = useValidations(alertId);

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const lab_results: LabResults = {};
    for (const f of FIELDS) {
      const raw = lab[f.key];
      if (raw !== undefined && raw !== "") (lab_results as Record<string, number>)[f.key] = Number(raw);
    }
    submit.mutate(
      {
        alert_id: alertId,
        sampled_on: sampledOn,
        lab_results,
        observed_condition: condition || null,
        notes: notes || null,
        submitted_by: by || null,
      },
      {
        onSuccess: async (v) => {
          setLab({});
          if (photo) {
            setPhotoState("uploading photo…");
            try {
              await api.validations.uploadPhoto(v.id, photo);
              setPhotoState("photo attached");
              void existing.refetch();
            } catch (err) {
              setPhotoState(`photo failed: ${err instanceof Error ? err.message : String(err)}`);
            }
            setPhoto(null);
          }
        },
      },
    );
  };

  return (
    <div className="space-y-4">
      <form onSubmit={onSubmit} className="space-y-3 rounded-md border p-3">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <label className="space-y-1">
            <span className="text-muted-foreground">Sampled on</span>
            <input type="date" required value={sampledOn} onChange={(e) => setSampledOn(e.target.value)} className="w-full rounded border px-2 py-1" />
          </label>
          <label className="space-y-1">
            <span className="text-muted-foreground">Submitted by</span>
            <input value={by} onChange={(e) => setBy(e.target.value)} placeholder="RO Pune field team" className="w-full rounded border px-2 py-1" />
          </label>
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
          {FIELDS.map((f) => (
            <label key={f.key} className="space-y-1">
              <span className="text-muted-foreground">
                {f.label} {f.unit && <span className="opacity-70">({f.unit})</span>}
              </span>
              <input
                type="number"
                step={f.step ?? "any"}
                min={0}
                value={lab[f.key] ?? ""}
                onChange={(e) => setLab({ ...lab, [f.key]: e.target.value })}
                className="w-full rounded border px-2 py-1 font-mono"
              />
            </label>
          ))}
        </div>
        <label className="block space-y-1 text-xs">
          <span className="text-muted-foreground">Observed condition</span>
          <input value={condition} onChange={(e) => setCondition(e.target.value)} placeholder="Brown plume near the eastern inlet, no odour." className="w-full rounded border px-2 py-1" />
        </label>
        <label className="block space-y-1 text-xs">
          <span className="text-muted-foreground">Notes</span>
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} className="w-full rounded border px-2 py-1" />
        </label>
        <label className="block space-y-1 text-xs">
          <span className="text-muted-foreground">Site photo (JPEG / PNG / WebP, optional)</span>
          <input type="file" accept="image/jpeg,image/png,image/webp" onChange={(e) => setPhoto(e.target.files?.[0] ?? null)} className="block w-full text-xs" />
        </label>
        <div className="flex items-center gap-3">
          <Button type="submit" disabled={submit.isPending}>
            {submit.isPending ? "Submitting…" : "Submit validation"}
          </Button>
          {submit.isSuccess && (
            <span className="flex items-center gap-1 text-xs text-emerald-700">
              <CheckCircle2 className="h-3.5 w-3.5" /> Recorded
              {submit.data.verdict ? ` · verdict ${submit.data.verdict}` : " · verdict pending"}
              {submit.data.verdict_reason && (
                <span className="text-muted-foreground"> — {submit.data.verdict_reason}</span>
              )}
            </span>
          )}
          {submit.error && <span className="text-xs text-destructive">{submit.error.message}</span>}
          {photoState && <span className="text-xs text-muted-foreground">{photoState}</span>}
        </div>
      </form>

      <div>
        <div className="mb-1 text-xs font-semibold">Previous validations</div>
        {existing.isLoading && <PanelSkeleton rows={2} />}
        {existing.error && <ErrorState error={existing.error} onRetry={() => void existing.refetch()} />}
        {existing.data && existing.data.items.length === 0 && (
          <div className="text-xs text-muted-foreground">None yet.</div>
        )}
        <ul className="space-y-1 text-xs">
          {existing.data?.items.map((v) => (
            <li key={v.id} className="rounded border px-2 py-1">
              <span className="font-medium">{fmtDate(v.sampled_on)}</span>
              {v.submitted_by && <span className="text-muted-foreground"> · {v.submitted_by}</span>}
              <span className="text-muted-foreground"> · submitted {fmtDateTime(v.created_at)}</span>
              <span className="ml-2 rounded bg-muted px-1.5 py-0.5" title={v.verdict_reason ?? ""}>
                {v.verdict ?? "verdict pending"}
              </span>
              {v.photo_url && (
                <a href={v.photo_url} target="_blank" rel="noreferrer" className="ml-2 text-primary hover:underline">
                  photo
                </a>
              )}
              {Object.keys(v.lab_results).length > 0 && (
                <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                  {Object.entries(v.lab_results)
                    .map(([k, val]) => `${k}=${String(val)}`)
                    .join("  ")}
                </div>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
