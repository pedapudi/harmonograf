// React selector hooks over PlanHistoryRegistry.
//
// These are the stable API the Task/Plan panel (harmonograf-plan-panel)
// and the Trajectory view (harmonograf-trajectory) build against. The
// signatures are frozen by the planning doc; any change here must be
// coordinated with both renderer agents.
//
// All hooks:
//   - Resolve the SessionStore via `getSessionStore(sessionId)`.
//   - Subscribe to `store.planHistory` for re-render on every append /
//     clear.
//   - Return `null` / empty when data isn't loaded yet. Renderers
//     branch on null to show a skeleton / placeholder.
//
// The hooks are pure projections over the registry — no caching, no
// memoized data structures on their own. Renderers that need
// referential stability over expensive derived data should wrap the
// return values in their own `useMemo`.

import { useEffect, useState } from 'react';
import type { TaskPlan } from '../gantt/types';
import { getSessionStore } from '../rpc/hooks';
import type {
  CumulativePlan,
  PlanRevisionRecord,
  SupersessionLink,
} from './planHistoryStore';

// Shared tick/subscribe shim. Mirrors the pattern used elsewhere in
// the app (useAgentLive, CurrentTaskStrip): force a re-render whenever
// the registry emits, and re-read from the live store on every render.
function usePlanHistoryTick(sessionId: string | null): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!sessionId) return;
    const store = getSessionStore(sessionId);
    if (!store) return;
    return store.planHistory.subscribe(() => setTick((t) => t + 1));
  }, [sessionId]);
}

/**
 * All revisions for a plan, sorted by revision number ascending.
 * Returns `[]` when the session isn't loaded or the plan id is unknown.
 */
export function usePlanHistory(
  sessionId: string | null,
  planId: string | null,
): PlanRevisionRecord[] {
  usePlanHistoryTick(sessionId);
  if (!sessionId || !planId) return EMPTY_RECORDS;
  const store = getSessionStore(sessionId);
  if (!store) return EMPTY_RECORDS;
  return store.planHistory.historyFor(planId);
}

/**
 * The Plan snapshot at an exact revision. Returns `null` if the
 * session isn't loaded, the plan id is unknown, or the revision
 * hasn't been recorded.
 */
export function usePlanAtRevision(
  sessionId: string | null,
  planId: string | null,
  revision: number,
): TaskPlan | null {
  usePlanHistoryTick(sessionId);
  if (!sessionId || !planId) return null;
  const store = getSessionStore(sessionId);
  if (!store) return null;
  return store.planHistory.planAtRevision(planId, revision);
}

/**
 * Cumulative plan (union of tasks across all revisions) with per-task
 * revision metadata. Returns `null` when no revisions have been seen
 * yet. The returned object is a fresh reference on every render (the
 * registry rebuilds it on demand) — memoize at the call site if
 * reference stability matters.
 */
export function useCumulativePlan(
  sessionId: string | null,
  planId: string | null,
): CumulativePlan | null {
  usePlanHistoryTick(sessionId);
  if (!sessionId || !planId) return null;
  const store = getSessionStore(sessionId);
  if (!store) return null;
  return store.planHistory.cumulativePlan(planId);
}

/**
 * Map of oldTaskId → SupersessionLink. Empty map when no revisions
 * have replaced tasks yet (initial plan, or revisions that only edit
 * existing tasks in place).
 */
export function useSupersedesMap(
  sessionId: string | null,
  planId: string | null,
): Map<string, SupersessionLink> {
  usePlanHistoryTick(sessionId);
  if (!sessionId || !planId) return EMPTY_LINKS;
  const store = getSessionStore(sessionId);
  if (!store) return EMPTY_LINKS;
  return store.planHistory.supersedesMap(planId);
}

const EMPTY_RECORDS: PlanRevisionRecord[] = Object.freeze(
  [],
) as unknown as PlanRevisionRecord[];
const EMPTY_LINKS: Map<string, SupersessionLink> = new Map();
