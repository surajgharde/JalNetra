/**
 * Minimal toast queue (no external dependency): a handful of dismissible
 * notices in the corner, auto-cleared after a few seconds. Deliberately not a
 * full toast library -- this app needs exactly one use so far (job started/
 * failed feedback for the "Fetch satellite data" action).
 */
import { create } from "zustand";

export interface ToastItem {
  id: number;
  message: string;
  tone: "info" | "success" | "error";
}

interface ToastState {
  items: ToastItem[];
  push: (message: string, tone?: ToastItem["tone"]) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;
const AUTO_DISMISS_MS = 5000;

export const useToasts = create<ToastState>((set, get) => ({
  items: [],
  push: (message, tone = "info") => {
    const id = nextId++;
    set((s) => ({ items: [...s.items, { id, message, tone }] }));
    window.setTimeout(() => get().dismiss(id), AUTO_DISMISS_MS);
  },
  dismiss: (id) => set((s) => ({ items: s.items.filter((t) => t.id !== id) })),
}));
