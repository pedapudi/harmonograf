// Unified intervention history, fetched from the server.
//
// The merge / attribution / condition-collapse logic lives server-side in
// the ``ListInterventions`` RPC (harmonograf_server/interventions.py) — the
// single source of truth. This store fetches the merged rows on session
// open and re-fetches (debounced) whenever a delta that could change the
// history lands on the WatchSession stream. Rows are replaced wholesale on
// each fetch; React consumers subscribe via ``useInterventions``.

import { useEffect } from 'react';
import { create } from 'zustand';

import type { SessionStore } from '../gantt/index';
import { getHarmonografClient } from '../rpc/client';
import { useAnnotationStore } from './annotationStore';
import type { InterventionRow } from '../lib/interventions';
import { interventionRowFromPb } from '../lib/interventions';

// Stable empty reference so the zustand selector returns a cached value
// (a fresh ``[]`` each render would trip useSyncExternalStore's loop guard).
// Typed mutable but frozen — it is only ever read, never mutated.
const EMPTY: InterventionRow[] = Object.freeze([]) as unknown as InterventionRow[];

interface ClientLike {
  listInterventions?: (req: {
    sessionId: string;
  }) => Promise<{ interventions?: unknown[] }>;
}

interface InterventionsState {
  bySession: Map<string, InterventionRow[]>;
  list(sessionId: string): InterventionRow[];
  refresh(sessionId: string, sessionStartMs: number): Promise<void>;
}

// Per-session coalescing: at most one in-flight fetch; a request that
// arrives during a fetch schedules exactly one follow-up with the latest
// sessionStartMs (matches the "run exactly one more after it completes"
// contract so a burst of deltas collapses to two fetches at most).
const inflight = new Set<string>();
const pending = new Map<string, number>();

export const useInterventionsStore = create<InterventionsState>((set, get) => ({
  bySession: new Map(),

  list(sessionId) {
    return get().bySession.get(sessionId) ?? EMPTY;
  },

  async refresh(sessionId, sessionStartMs) {
    if (!sessionId) return;
    if (inflight.has(sessionId)) {
      pending.set(sessionId, sessionStartMs);
      return;
    }
    inflight.add(sessionId);
    try {
      const client = getHarmonografClient() as unknown as ClientLike;
      if (typeof client.listInterventions !== 'function') return;
      const resp = await client.listInterventions({ sessionId });
      const rows = ((resp?.interventions ?? []) as Parameters<
        typeof interventionRowFromPb
      >[0][]).map((iv) => interventionRowFromPb(iv, sessionStartMs));
      set((state) => {
        const next = new Map(state.bySession);
        next.set(sessionId, rows);
        return { bySession: next };
      });
    } catch {
      // Best-effort: leave the prior rows in place on transient errors.
    } finally {
      inflight.delete(sessionId);
      const followUp = pending.get(sessionId);
      if (followUp !== undefined) {
        pending.delete(sessionId);
        void get().refresh(sessionId, followUp);
      }
    }
  },
}));

/**
 * Subscribe a component to the server-derived intervention rows for
 * ``sessionId`` and keep them fresh: fetch once on mount, then re-fetch
 * (debounced 300ms) whenever a WatchSession delta that could change the
 * merged history lands. Returns the current rows.
 */
export function useInterventions(
  store: SessionStore | null,
  sessionId: string,
): InterventionRow[] {
  const rows = useInterventionsStore((s) => s.list(sessionId));
  const refresh = useInterventionsStore((s) => s.refresh);

  useEffect(() => {
    if (!store || !sessionId) return;
    const trigger = () => void refresh(sessionId, store.wallClockStartMs);
    // Fetch immediately, then re-fetch on the next microtask so an initial
    // burst that lands wallClockStartMs (rebase) is picked up with the
    // correct session-relative axis.
    trigger();

    let timer: ReturnType<typeof setTimeout> | undefined;
    const debounced = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(trigger, 300);
    };
    const unsubs = [
      // spans catch the session-frame / rebase that lands wallClockStartMs.
      store.spans.subscribe(debounced),
      store.tasks.subscribe(debounced),
      store.drifts.subscribe(debounced),
      store.invocationCancels.subscribe(debounced),
      store.refineAttempts.subscribe(debounced),
      store.refineFailures.subscribe(debounced),
      store.taskTransitions.subscribe(debounced),
      store.userMessages.subscribe(debounced),
      useAnnotationStore.subscribe(debounced),
    ];
    return () => {
      if (timer) clearTimeout(timer);
      for (const un of unsubs) un();
    };
  }, [store, sessionId, refresh]);

  return rows;
}
