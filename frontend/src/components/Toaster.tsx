import { CheckCircle2, Info, X, XCircle } from "lucide-react";
import { useToasts } from "@/store/toast";
import { cn } from "@/lib/utils";

/** Mounted once at the app root; renders whatever `useToasts` currently holds. */
export function Toaster() {
  const items = useToasts((s) => s.items);
  const dismiss = useToasts((s) => s.dismiss);
  if (!items.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[4000] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2">
      {items.map((t) => (
        <div
          key={t.id}
          className={cn(
            "pointer-events-auto flex items-start gap-2 rounded-md border bg-card px-3 py-2 text-xs shadow-lg",
            t.tone === "success" && "border-emerald-300",
            t.tone === "error" && "border-red-300",
          )}
        >
          {t.tone === "success" ? (
            <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600" />
          ) : t.tone === "error" ? (
            <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
          ) : (
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
          )}
          <span className="min-w-0 flex-1">{t.message}</span>
          <button
            onClick={() => dismiss(t.id)}
            className="shrink-0 text-muted-foreground hover:text-foreground"
            aria-label="Dismiss"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}
