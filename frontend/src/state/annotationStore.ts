import { create } from 'zustand';

export type AnnotationKind = 'COMMENT' | 'STEERING' | 'HUMAN_RESPONSE';

export interface Annotation {
  id: string;
  sessionId: string;
  spanId: string | null;
  agentId: string | null;
  // Session-relative ms. For span-targeted annotations, this is the span's
  // startMs; for agent-time annotations, the chosen time.
  atMs: number;
  // Optional end time for range annotations authored via click-drag on the
  // Gantt. Proto only carries a point target, so the end is client-local until
  // a future schema revision lets us round-trip it through the server.
  rangeEndMs?: number | null;
  author: string;
  kind: AnnotationKind;
  body: string;
  createdAtMs: number;
  deliveredAtMs: number | null;
  // Client-side state for optimistic updates.
  pending: boolean;
  // If the optimistic insert failed, `error` is set and the row is rendered
  // in a failure state until the user retries or dismisses.
  error: string | null;
}

interface AnnotationState {
  bySession: Map<string, Annotation[]>;
  list(sessionId: string): Annotation[];
  listForSpan(sessionId: string, spanId: string): Annotation[];
  upsert(a: Annotation): void;
  remove(sessionId: string, id: string): void;
  markError(sessionId: string, id: string, error: string): void;
  markDelivered(sessionId: string, id: string, deliveredAtMs: number): void;
}

export const useAnnotationStore = create<AnnotationState>((set, get) => ({
  bySession: new Map(),

  list(sessionId) {
    return get().bySession.get(sessionId) ?? [];
  },

  listForSpan(sessionId, spanId) {
    const arr = get().bySession.get(sessionId) ?? [];
    return arr.filter((a) => a.spanId === spanId);
  },

  upsert(a) {
    set((state) => {
      const next = new Map(state.bySession);
      const arr = (next.get(a.sessionId) ?? []).slice();
      const idx = arr.findIndex((x) => x.id === a.id);
      if (idx >= 0) arr[idx] = { ...arr[idx], ...a };
      else arr.push(a);
      arr.sort((x, y) => x.atMs - y.atMs);
      next.set(a.sessionId, arr);
      return { bySession: next };
    });
  },

  remove(sessionId, id) {
    set((state) => {
      const next = new Map(state.bySession);
      const arr = (next.get(sessionId) ?? []).filter((a) => a.id !== id);
      next.set(sessionId, arr);
      return { bySession: next };
    });
  },

  markError(sessionId, id, error) {
    set((state) => {
      const next = new Map(state.bySession);
      const arr = (next.get(sessionId) ?? []).map((a) =>
        a.id === id ? { ...a, error, pending: false } : a,
      );
      next.set(sessionId, arr);
      return { bySession: next };
    });
  },

  markDelivered(sessionId, id, deliveredAtMs) {
    set((state) => {
      const next = new Map(state.bySession);
      const arr = (next.get(sessionId) ?? []).map((a) =>
        a.id === id ? { ...a, deliveredAtMs, pending: false } : a,
      );
      next.set(sessionId, arr);
      return { bySession: next };
    });
  },
}));
