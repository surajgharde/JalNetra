import { ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Renders the `disclaimer` field exactly as the API returned it. The text is
 * never hardcoded here and this component never hides it: if the API did not
 * send one, that is a bug worth seeing on screen.
 */
export function Disclaimer({ text, className }: { text: string | undefined; className?: string }) {
  return (
    <p
      role="note"
      className={cn(
        "flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900",
        className,
      )}
    >
      <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{text ?? "(disclaimer missing from API response)"}</span>
    </p>
  );
}
