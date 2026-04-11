import { create } from 'zustand';

// Floating span popover state. Multiple popovers can coexist when pinned;
// unpinned popovers are transient — clicking a different span swaps the
// single unpinned slot, and a click-away dismisses it.
//
// Anchor coordinates (anchorX/anchorY) are canvas-local CSS pixels captured
// at click time. The popover component re-anchors to the span's current
// rectangle each render via the renderer, so popovers track their span
// through pans, zooms, and resizes — the stored anchor is only used as a
// fallback (offscreen span, span removed).

export interface SpanPopover {
  spanId: string;
  pinned: boolean;
  anchorX: number;
  anchorY: number;
}

interface PopoverState {
  popovers: Map<string, SpanPopover>;

  // Click on a span: if a popover for this span exists, leave it; otherwise
  // open a new unpinned popover, evicting any other unpinned popover first.
  openForSpan: (spanId: string, anchorX: number, anchorY: number) => void;
  togglePin: (spanId: string) => void;
  close: (spanId: string) => void;
  closeUnpinned: () => void;
  closeAll: () => void;
}

export const usePopoverStore = create<PopoverState>((set) => ({
  popovers: new Map(),

  openForSpan: (spanId, anchorX, anchorY) =>
    set((s) => {
      const next = new Map(s.popovers);
      const existing = next.get(spanId);
      if (existing) {
        existing.anchorX = anchorX;
        existing.anchorY = anchorY;
        return { popovers: next };
      }
      for (const [id, p] of next) {
        if (!p.pinned) next.delete(id);
      }
      next.set(spanId, { spanId, pinned: false, anchorX, anchorY });
      return { popovers: next };
    }),

  togglePin: (spanId) =>
    set((s) => {
      const existing = s.popovers.get(spanId);
      if (!existing) return s;
      const next = new Map(s.popovers);
      next.set(spanId, { ...existing, pinned: !existing.pinned });
      return { popovers: next };
    }),

  close: (spanId) =>
    set((s) => {
      if (!s.popovers.has(spanId)) return s;
      const next = new Map(s.popovers);
      next.delete(spanId);
      return { popovers: next };
    }),

  closeUnpinned: () =>
    set((s) => {
      let changed = false;
      const next = new Map(s.popovers);
      for (const [id, p] of next) {
        if (!p.pinned) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? { popovers: next } : s;
    }),

  closeAll: () => set({ popovers: new Map() }),
}));
