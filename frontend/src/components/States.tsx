import { AlertTriangle, Inbox, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

export function PanelSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-3" aria-busy="true">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-5 w-full" />
      ))}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 p-6 text-center text-sm text-muted-foreground">
      <Inbox className="h-6 w-6 opacity-60" />
      <div className="font-medium text-foreground">{title}</div>
      {hint && <div className="text-xs">{hint}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="flex flex-col items-center justify-center gap-2 p-6 text-center text-sm">
      <AlertTriangle className="h-6 w-6 text-destructive" />
      <div className="font-medium">Could not load this panel</div>
      <div className="max-w-xs text-xs text-muted-foreground">{message}</div>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          <RefreshCw className="h-3 w-3" /> Retry
        </Button>
      )}
    </div>
  );
}

/**
 * `?wb=` is a shareable link, so a stale or mistyped id must say so rather than
 * render an empty dashboard that reads like a healthy water body.
 */
export function NotFoundState({ id }: { id: string }) {
  return (
    <EmptyState
      title="Water body not found"
      hint={`Nothing in the registry with id "${id}". It may have been removed — pick one from the bar above.`}
    />
  );
}
