// Plan-history selector hooks exposed to the Trajectory view + Task/Plan
// panel. Signatures are frozen per the sibling `/tmp/harmonograf-plan-state`
// design doc so that downstream renderers can be built in parallel.
//
//   useCumulativePlan(sessionId, planId)  → CumulativePlan | null
//   useSupersedesMap(sessionId, planId)   → Map<oldTaskId, SupersessionLink>
//   usePlanHistory(sessionId, planId)     → readonly PlanRevisionRecord[]
//
// Implementation is dual-sourced:
//
//   1. Authoritative: `SessionStore.planHistory` — fed by the upcoming
//      `GetSessionPlanHistory` RPC + live plan_submitted / plan_revised
//      stream. When the RPC lands, these hooks see it automatically.
//
//   2. Bootstrap: when the registry is empty for `planId` (the pre-RPC
//      world), seed it from `SessionStore.tasks.allRevsForPlan(planId)`.
//      TaskRegistry already keeps the prior snapshots in a rolling list,
//      so the Trajectory view can render evolution + steering today
//      without waiting on the plan-history RPC to merge. The bootstrap is
//      idempotent on (plan_id, revision), so it harmlessly no-ops once
//      the real producer starts emitting.

import { useEffect, useState } from 'react';
import type { SessionStore } from '../gantt/index';
import type {
  CumulativePlan,
  PlanRevisionRecord,
  SupersessionLink,
} from './planHistoryStore';
import { useSessionWatch } from '../rpc/hooks';

// Bridge TaskRegistry snapshots into PlanHistoryRegistry. Idempotent on
// (plan_id, revision) — safe to re-run on every render / subscribe tick.
// Pulls rev / kind / reason / triggerEventId off each TaskPlan (populated
// by the goldfive event ingest at rpc/goldfiveEvent.ts); the triggering
// drift's detail is used as the reason when the plan itself has none.
function bootstrapFromTasks(store: SessionStore, planId: string): void {
  const revs = store.tasks.allRevsForPlan(planId);
  if (revs.length === 0) return;
  for (const plan of revs) {
    const revision = plan.revisionIndex ?? 0;
    const reason = plan.revisionReason ?? '';
    const kind = plan.revisionKind ?? '';
    const triggerEventId = plan.triggerEventId ?? '';
    store.planHistory.append({
      revision,
      plan,
      reason,
      kind,
      triggerEventId,
      emittedAtMs: plan.createdAtMs,
    });
  }
}

// Subscribe to both planHistory + tasks so we re-render on either
// (a) direct RPC/live append, or (b) a new TaskRegistry rev (which the
// bootstrap below reflects into planHistory on the next read).
function usePlanHistoryTick(store: SessionStore | null): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!store) return;
    const un1 = store.planHistory.subscribe(() => setTick((n) => n + 1));
    const un2 = store.tasks.subscribe(() => setTick((n) => n + 1));
    return () => {
      un1();
      un2();
    };
  }, [store]);
  return tick;
}

/**
 * Latest CumulativePlan for (sessionId, planId), or null when the plan
 * is unknown. Tasks the latest revision dropped are retained in the
 * returned plan with `taskRevisionMeta.isSuperseded = true` so the DAG
 * renderer can keep them as historical nodes.
 */
export function useCumulativePlan(
  sessionId: string | null,
  planId: string | null,
): CumulativePlan | null {
  const watch = useSessionWatch(sessionId);
  const store = sessionId ? watch.store : null;
  usePlanHistoryTick(store);
  if (!store || !planId) return null;
  bootstrapFromTasks(store, planId);
  return store.planHistory.cumulativePlan(planId);
}

/**
 * Old → new replacement links, keyed by the old (superseded) task id.
 * Empty map when fewer than two revisions exist, or when no tasks were
 * dropped across the revision chain.
 */
export function useSupersedesMap(
  sessionId: string | null,
  planId: string | null,
): Map<string, SupersessionLink> {
  const watch = useSessionWatch(sessionId);
  const store = sessionId ? watch.store : null;
  usePlanHistoryTick(store);
  if (!store || !planId) return new Map();
  bootstrapFromTasks(store, planId);
  return store.planHistory.supersedesMap(planId);
}

/**
 * Ordered list of PlanRevisionRecords for (sessionId, planId), oldest
 * first. Empty array when the plan is unknown.
 */
export function usePlanHistory(
  sessionId: string | null,
  planId: string | null,
): readonly PlanRevisionRecord[] {
  const watch = useSessionWatch(sessionId);
  const store = sessionId ? watch.store : null;
  usePlanHistoryTick(store);
  if (!store || !planId) return EMPTY_HISTORY;
  bootstrapFromTasks(store, planId);
  return store.planHistory.historyFor(planId);
}

const EMPTY_HISTORY: readonly PlanRevisionRecord[] = Object.freeze([]);
