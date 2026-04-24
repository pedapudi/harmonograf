// Plan-history adapters (harmonograf: plan-panel-evolution).
//
// This module exposes the hook surface frozen by the sibling PR
// `/tmp/harmonograf-plan-state` — built locally against the existing
// `TaskRegistry.allRevsForPlan()` infrastructure so the renderer work in
// this branch has real data to consume pre-merge. Once the state sibling
// lands, this file is swapped for the one that reads `SessionStore.planHistory`
// directly; the exported types and hook signatures stay frozen.
//
// Consumed types:
//   * PlanRevisionRecord  — one entry per revision, oldest-first.
//   * CumulativePlan       — union-DAG across revisions with per-task metadata
//                            (introduced-in / last-modified-in / isSuperseded).
//   * SupersessionLink     — per-retired-task reference to its replacement +
//                            the triggering drift.
//
// Hook signatures (frozen — do not rename or restructure):
//   usePlanHistory(sessionId, planId)   → readonly PlanRevisionRecord[]
//   useCumulativePlan(sessionId, planId) → CumulativePlan | null
//   useSupersedesMap(sessionId, planId)  → Map<oldTaskId, SupersessionLink>
//
// All three are reactive: they subscribe to the session's TaskRegistry +
// DriftRegistry so React rerenders when a new revision arrives.

import { useEffect, useState } from 'react';
import { getSessionStore } from '../rpc/hooks';
import type { SessionStore } from '../gantt/index';
import type { DriftRecord } from '../gantt/index';
import type { Task, TaskEdge, TaskPlan } from '../gantt/types';

// One entry per revision of a plan. The most recent revision sits at the
// tail. `revisionIndex` is the authoritative ordering field (0 = initial).
export interface PlanRevisionRecord {
  planId: string;
  revisionIndex: number;
  revisedAtMs: number;        // session-relative
  revisionKind: string;        // e.g. OFF_TOPIC, USER_STEER, '' for initial
  revisionReason: string;
  triggerEventId: string;
  plan: TaskPlan;              // snapshot at this revision
}

// Per-task meta produced by the cumulative projection.
export interface TaskRevisionMeta {
  introducedInRevision: number;
  lastModifiedInRevision: number;
  isSuperseded: boolean;
  // Id of the task that replaced this one, if superseded.
  supersededByTaskId: string | null;
}

// Cumulative (union) DAG across every revision of a plan. Tasks dropped
// by a later revision are kept as `isSuperseded=true` so the renderer can
// show the "evolved" plan in a single picture.
export interface CumulativePlan {
  planId: string;
  latestRevisionIndex: number;
  tasks: Task[];               // union across revisions, de-duped by id
  edges: TaskEdge[];           // union across revisions, de-duped
  taskRevisionMeta: Map<string, TaskRevisionMeta>;
}

// Pointer from a retired task to the task that replaced it. Populated for
// every task whose id dropped out between adjacent revisions AND whose
// replacement we can identify (by position, by title affinity, or — when
// goldfive stamps `refine.supersedes`, post-merge — explicit id mapping).
export interface SupersessionLink {
  oldTaskId: string;
  newTaskId: string;
  revision: number;         // revisionIndex where the supersession happened
  kind: string;             // drift kind (e.g. OFF_TOPIC) or ''
  reason: string;           // revisionReason text
  triggerEventId: string;   // drift id when goldfive-driven, '' otherwise
  // Authorship: 'user' (USER_STEER etc.), 'goldfive' (autonomous), or ''.
  authoredBy: string;
}

// ── Low-level derivation (pure functions over a snapshot list) ───────────────

// Materialise a PlanRevisionRecord list from the registry's rev sequence.
// `revs` is oldest-first.
function deriveHistory(
  planId: string,
  revs: ReadonlyArray<TaskPlan>,
): PlanRevisionRecord[] {
  const out: PlanRevisionRecord[] = [];
  for (const plan of revs) {
    out.push({
      planId,
      revisionIndex: plan.revisionIndex ?? 0,
      revisedAtMs: plan.createdAtMs,
      revisionKind: plan.revisionKind || '',
      revisionReason: plan.revisionReason || '',
      triggerEventId: plan.triggerEventId || '',
      plan,
    });
  }
  return out;
}

// Fold every revision into one cumulative DAG. Tasks are keyed by id;
// the latest non-null snapshot wins. Superseded tasks come from revisions
// where the id existed but dropped out in a later one.
function deriveCumulative(
  planId: string,
  revs: ReadonlyArray<TaskPlan>,
): CumulativePlan | null {
  if (revs.length === 0) return null;
  const latestRevisionIndex = revs[revs.length - 1].revisionIndex ?? revs.length - 1;
  const taskSnapshots = new Map<string, Task>();         // id → latest snapshot
  const introducedInRevision = new Map<string, number>();
  const lastModifiedInRevision = new Map<string, number>();
  const edgeKeys = new Set<string>();
  const edges: TaskEdge[] = [];
  const everPresent = new Map<string, Set<number>>();    // id → {revIdxs that had it}

  for (const rev of revs) {
    const revIdx = rev.revisionIndex ?? 0;
    for (const t of rev.tasks) {
      const prior = taskSnapshots.get(t.id);
      const introducedAt = introducedInRevision.get(t.id);
      if (introducedAt === undefined) {
        introducedInRevision.set(t.id, revIdx);
      }
      // Track when a task's content (title/status/assignee/description)
      // last changed. Status changes do NOT count as a revision bump
      // unless the plan was re-emitted (goldfive publishes a fresh plan
      // for every refine).
      if (
        !prior ||
        prior.title !== t.title ||
        prior.description !== t.description ||
        prior.assigneeAgentId !== t.assigneeAgentId
      ) {
        lastModifiedInRevision.set(t.id, revIdx);
      } else if (!lastModifiedInRevision.has(t.id)) {
        lastModifiedInRevision.set(t.id, revIdx);
      }
      taskSnapshots.set(t.id, t);
      let seen = everPresent.get(t.id);
      if (!seen) {
        seen = new Set();
        everPresent.set(t.id, seen);
      }
      seen.add(revIdx);
    }
    for (const e of rev.edges) {
      const k = `${e.fromTaskId}->${e.toTaskId}`;
      if (!edgeKeys.has(k)) {
        edgeKeys.add(k);
        edges.push(e);
      }
    }
  }

  // A task is "superseded" if it no longer appears in the final revision.
  // Use the latest revision's task id set as the ground truth for
  // "currently present".
  const latestRev = revs[revs.length - 1];
  const latestTaskIds = new Set<string>();
  for (const t of latestRev.tasks) latestTaskIds.add(t.id);

  const taskRevisionMeta = new Map<string, TaskRevisionMeta>();
  const tasks: Task[] = [];
  for (const [id, t] of taskSnapshots) {
    tasks.push(t);
    taskRevisionMeta.set(id, {
      introducedInRevision: introducedInRevision.get(id) ?? 0,
      lastModifiedInRevision: lastModifiedInRevision.get(id) ?? 0,
      isSuperseded: !latestTaskIds.has(id),
      supersededByTaskId: null, // filled in by deriveSupersedes
    });
  }

  return {
    planId,
    latestRevisionIndex,
    tasks,
    edges,
    taskRevisionMeta,
  };
}

// Pair retired task ids with their replacements. Strategy:
//   1. Pairwise between adjacent revs: `retired` = ids present in rev[i]
//      but absent in rev[i+1]; `added` = ids present in rev[i+1] but
//      absent in rev[i].
//   2. Prefer an explicit mapping from `t.supersedesTaskId` if goldfive
//      stamps it (post-merge). Pre-merge we fall back to title-affinity
//      (same assignee + title token overlap) and then to position.
//   3. The drift record for this revision provides the `kind` + `reason`.
function deriveSupersedes(
  revs: ReadonlyArray<TaskPlan>,
  driftsById: Map<string, DriftRecord>,
): Map<string, SupersessionLink> {
  const out = new Map<string, SupersessionLink>();
  for (let i = 1; i < revs.length; i++) {
    const prev = revs[i - 1];
    const next = revs[i];
    const prevIds = new Set(prev.tasks.map((t) => t.id));
    const nextIds = new Set(next.tasks.map((t) => t.id));
    const retired: Task[] = [];
    const added: Task[] = [];
    for (const t of prev.tasks) if (!nextIds.has(t.id)) retired.push(t);
    for (const t of next.tasks) if (!prevIds.has(t.id)) added.push(t);
    if (retired.length === 0) continue;
    // Pick the drift record that triggered this revision, if any.
    const drift = next.triggerEventId
      ? driftsById.get(next.triggerEventId)
      : undefined;
    const revIdx = next.revisionIndex ?? i;
    // Pair retired ↔ added. Deterministic order: for each retired task,
    // prefer an added task whose title has the most overlap (>= 2 shared
    // tokens excluding stopwords), else same assignee, else positional.
    const claimed = new Set<number>();
    for (const old of retired) {
      let bestIdx = -1;
      let bestScore = 0;
      for (let j = 0; j < added.length; j++) {
        if (claimed.has(j)) continue;
        const cand = added[j];
        let score = 0;
        if (cand.assigneeAgentId === old.assigneeAgentId) score += 1;
        score += titleAffinity(old.title, cand.title);
        if (score > bestScore) {
          bestScore = score;
          bestIdx = j;
        }
      }
      // If nothing scored, fall back to positional — retired[k] → added[k].
      if (bestIdx < 0) {
        const idx = retired.indexOf(old);
        if (idx < added.length && !claimed.has(idx)) bestIdx = idx;
      }
      if (bestIdx >= 0) {
        claimed.add(bestIdx);
        const replacement = added[bestIdx];
        out.set(old.id, {
          oldTaskId: old.id,
          newTaskId: replacement.id,
          revision: revIdx,
          kind: (next.revisionKind || drift?.kind || '').toString(),
          reason: next.revisionReason || drift?.detail || '',
          triggerEventId: next.triggerEventId || drift?.driftId || '',
          authoredBy: drift?.authoredBy || '',
        });
      } else {
        // Retired with no replacement: record the supersession without a
        // new task id so the UI can still mute the node + show the reason.
        out.set(old.id, {
          oldTaskId: old.id,
          newTaskId: '',
          revision: revIdx,
          kind: (next.revisionKind || drift?.kind || '').toString(),
          reason: next.revisionReason || drift?.detail || '',
          triggerEventId: next.triggerEventId || drift?.driftId || '',
          authoredBy: drift?.authoredBy || '',
        });
      }
    }
  }
  return out;
}

function titleAffinity(a: string, b: string): number {
  if (!a || !b) return 0;
  const aset = tokenize(a);
  const bset = tokenize(b);
  let shared = 0;
  for (const t of aset) if (bset.has(t)) shared++;
  return shared >= 2 ? 2 : shared;
}

const STOPWORDS = new Set([
  'a', 'an', 'and', 'or', 'the', 'to', 'of', 'in', 'on', 'for', 'with',
  'by', 'is', 'it', 'be', 'as', 'at', 'from',
]);

function tokenize(s: string): Set<string> {
  const out = new Set<string>();
  for (const raw of s.toLowerCase().split(/\W+/)) {
    if (!raw || STOPWORDS.has(raw) || raw.length < 3) continue;
    out.add(raw);
  }
  return out;
}

// Internal: build a drift-id lookup map from the session store's drift list.
function driftsByIdFor(store: SessionStore | null): Map<string, DriftRecord> {
  const m = new Map<string, DriftRecord>();
  if (!store) return m;
  for (const d of store.drifts.list()) {
    if (d.driftId) m.set(d.driftId, d);
    if (d.annotationId) m.set(d.annotationId, d);
  }
  return m;
}

// Pull the full rev sequence for a plan from the session store. Empty when
// the plan is unknown.
function revsFor(store: SessionStore | null, planId: string): ReadonlyArray<TaskPlan> {
  if (!store) return [];
  return store.tasks.allRevsForPlan(planId);
}

// ── Hooks (frozen signatures) ────────────────────────────────────────────────

// Re-render-on-store-change helper used by all three hooks. Subscribes to
// both the TaskRegistry (plan-rev updates) and the DriftRegistry (drift
// metadata used by the supersedes map).
function useStoreTick(sessionId: string | null): SessionStore | null {
  const store = sessionId ? getSessionStore(sessionId) ?? null : null;
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!store) return;
    const unT = store.tasks.subscribe(() => setTick((t) => t + 1));
    const unD = store.drifts.subscribe(() => setTick((t) => t + 1));
    return () => {
      unT();
      unD();
    };
  }, [store]);
  return store;
}

export function usePlanHistory(
  sessionId: string | null,
  planId: string | null,
): readonly PlanRevisionRecord[] {
  const store = useStoreTick(sessionId);
  if (!planId) return [];
  return deriveHistory(planId, revsFor(store, planId));
}

export function useCumulativePlan(
  sessionId: string | null,
  planId: string | null,
): CumulativePlan | null {
  const store = useStoreTick(sessionId);
  if (!planId) return null;
  return deriveCumulative(planId, revsFor(store, planId));
}

export function useSupersedesMap(
  sessionId: string | null,
  planId: string | null,
): Map<string, SupersessionLink> {
  const store = useStoreTick(sessionId);
  if (!planId) return new Map();
  return deriveSupersedes(revsFor(store, planId), driftsByIdFor(store));
}

// Pure-function variants exported for tests (they don't touch React or
// the global store cache). Tests seed a TaskRegistry directly and call
// these; the hook signatures above are thin reactive wrappers.
export const __internal = {
  deriveHistory,
  deriveCumulative,
  deriveSupersedes,
};
