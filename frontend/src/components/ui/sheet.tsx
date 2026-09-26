import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

/** Right-hand slide-over built on Radix Dialog. */
export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  children,
  className,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        {/* Leaflet's own panes and controls run up to z-index 1000, so this
            drawer needs a comfortable margin above that or it renders behind
            the map instead of over it. */}
        <Dialog.Overlay className="fixed inset-0 z-[1999] bg-slate-900/30 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <Dialog.Content
          className={cn(
            "fixed inset-y-0 right-0 z-[2000] flex w-full max-w-2xl flex-col overflow-y-auto border-l bg-card shadow-2xl pointer-events-auto data-[state=open]:animate-in data-[state=open]:slide-in-from-right",
            className,
          )}
        >
          <div className="flex items-start justify-between border-b px-5 py-3">
            <div>
              <Dialog.Title className="text-base font-semibold">{title}</Dialog.Title>
              {description && (
                <Dialog.Description className="text-xs text-muted-foreground">
                  {description}
                </Dialog.Description>
              )}
            </div>
            <Dialog.Close className="rounded p-1 text-muted-foreground hover:bg-muted" aria-label="Close">
              <X className="h-4 w-4" />
            </Dialog.Close>
          </div>
          <div className="flex-1 overflow-y-auto">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
