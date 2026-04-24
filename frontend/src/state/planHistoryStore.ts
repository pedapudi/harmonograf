// Plan-evolution registry: accumulates every revision of every plan in a
// session so the Task/Plan panel and the Trajectory view can render plans
// as evolving artifacts (generation badges, historical-dim styling,
// annotated supersedes edges) rather than a single live snapshot.
//
// `SessionStore.tasks` (TaskRegistry) keeps the *current* plan per id plus
// a rolling list of the last N past snapshots; that's sufficient for the
// existing PlanRevisionBanner + Trajectory walk, but it discards once-seen
// tasks as soon as a new revision lands, so the downstream renderers can't
// show "superseded" nodes without digging through the snapshot array.
//
// PlanHistoryRegistry keeps every revision keyed by (plan_id,
// revision_number) and derives three views on demand:
//
//   - `cumulativePlan(planId)` — the union of tasks across all revisions
//     with per-task `introducedInRevision` / `lastModifiedInRevision` /
//     `isSuperseded` metadata for generation badges.
//   - `supersedesMap(planId)` — per-task supersession links carrying the
//     drift kind / reason / trigger event id from the revision that
//     replaced each task. Used to draw annotated supersedes edges.
//   - `planAtRevision(planId, n)` — the Plan snapshot at an exact rev.
//
// All append paths are idempotent on (plan_id, revision_number), so the
// RPC snapshot loader and the live event stream can both populate the
// registry without worrying about double-insert.

import type { Task, TaskEdge, TaskPlan } from '../gantt/types';

export interface PlanRevisionRecord {
  /** 0 for the initial plan_submitted, 1..N for each plan_revised. */
  revision: number;
  /** Full Plan snapshot at this revision (deep-cloned on append). */
  plan: TaskPlan;
  /** Drift detail or user steer body ('' on the initial rev). */
  reason: string;
  /** DriftKind name ("off_topic" | "user_steer" | …); '' on initial. */
  kind: string;
  /** Goldfive drift/annotation id that triggered this rev ('' on initial). */
  triggerEventId: string;
  /** Wall-clock ms when the revision was recorded. */
  emittedAtMs: number;
}

export interface SupersessionLink {
  /** Task id that was replaced. */
  oldTaskId: string;
  /** Task id that replaced it. Empty string if the old task was dropped
   *  outright (renderers typically render this as a dangling "retired"
   *  edge anchored at the old task). */
  newTaskId: string;
  /** Revision number of the plan that introduced the replacement. */
  revision: number;
  /** DriftKind name of the triggering drift (e.g. "off_topic",
   *  "user_steer"). Empty when the surrounding revision had no kind. */
  kind: string;
  /** drift.detail or user steer body. */
  reason: string;
  /** Goldfive drift/annotation id tied to the triggering event. */
  triggerEventId: string;
}

export interface CumulativeTaskMeta {
  introducedInRevision: number;
  lastModifiedInRevision: number;
  /** True when the task id does NOT appear in the latest revision's
   *  task list — i.e. it was dropped or replaced somewhere along the
   *  revision chain. */
  isSuperseded: boolean;
}

export interface CumulativePlan extends TaskPlan {
  /** Per-task revision metadata. Keyed by Task.id. */
  taskRevisionMeta: Map<string, CumulativeTaskMeta>;
}

function clonePlan(p: TaskPlan): TaskPlan {
  return {
    ...p,
    tasks: p.tasks.map((t) => ({ ...t })),
    edges: p.edges.map((e) => ({ ...e })),
  };
}

function taskFingerprint(t: Task): string {
  // Fields that, when changed, mean a task was meaningfully edited by a
  // revision (vs. merely carried through unchanged). Mirrors the change
  // axes already tracked by computePlanDiff in gantt/index.ts so the two
  // diff notions stay consistent.
  return [
    t.title,
    t.description,
    t.assigneeAgentId,
    t.status,
    t.predictedStartMs,
    t.predictedDurationMs,
  ].join('');
}

/**
 * Accumulates every revision of every plan observed in a session.
 *
 * Lives on `SessionStore.planHistory` alongside the existing TaskRegistry.
 * Fed by two idempotent paths:
 *   A) `GetSessionPlanHistory` RPC snapshot at session load.
 *   B) Live `planSubmitted` / `planRevised` events from the goldfive
 *      event stream.
 * Both paths dedup on (plan_id, revision_number).
 */
export class PlanHistoryRegistry {
  private revisions = new Map<string, PlanRevisionRecord[]>();
  private listeners = new Set<() => void>();

  /**
   * Append a revision. Idempotent: a second append of the same
   * (plan.id, revision) is a no-op and does NOT emit. If the revision
   * arrives out of order (e.g. RPC replay + live tail race), it is
   * inserted in the correct sorted position.
   */
  append(record: PlanRevisionRecord): void {
    const planId = record.plan.id;
    if (!planId) return;
    let arr = this.revisions.get(planId);
    if (!arr) {
      arr = [];
      this.revisions.set(planId, arr);
    }
    // Idempotency on (plan_id, revision): skip exact-rev duplicates.
    if (arr.some((r) => r.revision === record.revision)) return;
    // Deep-clone the Plan so later mutations on the live TaskRegistry
    // don't leak into the historical snapshot. All other fields on
    // PlanRevisionRecord are primitives.
    const cloned: PlanRevisionRecord = { ...record, plan: clonePlan(record.plan) };
    // Linear insert keeping the array sorted by revision number.
    let i = arr.length;
    while (i > 0 && arr[i - 1].revision > cloned.revision) i--;
    if (i === arr.length) arr.push(cloned);
    else arr.splice(i, 0, cloned);
    this.emit();
  }

  /**
   * All revisions for a plan, sorted by revision number ascending.
   * Returns an empty array for unknown plan ids. The returned array is
   * a stable internal reference — callers must not mutate it.
   */
  historyFor(planId: string): PlanRevisionRecord[] {
    return this.revisions.get(planId) ?? EMPTY_REVISIONS;
  }

  /**
   * The Plan snapshot at an exact revision. Returns null when either
   * the plan id is unknown or no record exists for that revision.
   */
  planAtRevision(planId: string, revision: number): TaskPlan | null {
    const arr = this.revisions.get(planId);
    if (!arr) return null;
    const hit = arr.find((r) => r.revision === revision);
    return hit ? hit.plan : null;
  }

  /**
   * The cumulative plan: union of tasks across all revisions, most-recent
   * definition wins per id. Retains tasks that have been superseded
   * (marked `isSuperseded=true`) so the renderer can show them as
   * historical nodes. Edges are taken from the latest revision.
   *
   * Returns null when no revisions have been recorded for the plan id.
   */
  cumulativePlan(planId: string): CumulativePlan | null {
    const arr = this.revisions.get(planId);
    if (!arr || arr.length === 0) return null;
    const latest = arr[arr.length - 1].plan;
    const meta = new Map<string, CumulativeTaskMeta>();
    const unionById = new Map<string, Task>();
    const lastFingerprint = new Map<string, string>();
    for (const rev of arr) {
      for (const task of rev.plan.tasks) {
        const fp = taskFingerprint(task);
        const existingMeta = meta.get(task.id);
        if (!existingMeta) {
          meta.set(task.id, {
            introducedInRevision: rev.revision,
            lastModifiedInRevision: rev.revision,
            isSuperseded: false,
          });
        } else if (lastFingerprint.get(task.id) !== fp) {
          existingMeta.lastModifiedInRevision = rev.revision;
        }
        lastFingerprint.set(task.id, fp);
        // Most-recent definition wins (tasks mutate status across revs).
        unionById.set(task.id, { ...task });
      }
    }
    // Mark tasks that no longer appear in the latest revision.
    const latestIds = new Set(latest.tasks.map((t) => t.id));
    for (const [id, m] of meta) {
      if (!latestIds.has(id)) m.isSuperseded = true;
    }
    // Stable output ordering: latest revision's tasks first (preserving
    // planner-supplied order), then any superseded tasks in the order
    // they were first seen. This keeps the renderer's primary DAG stable
    // frame-to-frame and appends historical nodes at the tail.
    const tasks: Task[] = [];
    const emitted = new Set<string>();
    for (const t of latest.tasks) {
      const unioned = unionById.get(t.id);
      if (unioned) {
        tasks.push(unioned);
        emitted.add(t.id);
      }
    }
    for (const [id, t] of unionById) {
      if (!emitted.has(id)) tasks.push(t);
    }
    // Edge union: a task-level cumulative view should preserve edges
    // that were ever present so the renderer can draw historical
    // dependencies. Dedup by "from->to" and attach the latest occurrence.
    const edgeKeys = new Set<string>();
    const edges: TaskEdge[] = [];
    for (const rev of arr) {
      for (const e of rev.plan.edges) {
        const k = `${e.fromTaskId}->${e.toTaskId}`;
        if (edgeKeys.has(k)) continue;
        edgeKeys.add(k);
        edges.push({ fromTaskId: e.fromTaskId, toTaskId: e.toTaskId });
      }
    }
    return {
      ...latest,
      tasks,
      edges,
      taskRevisionMeta: meta,
    };
  }

  /**
   * Map of oldTaskId → SupersessionLink for every task that was
   * superseded or dropped across the revision chain.
   *
   * Interpretation: walking rev N → rev N+1, every task id that was in
   * N but not in N+1 is considered superseded by rev N+1. When rev N+1
   * introduces exactly one new task id (not present in any earlier
   * rev), that task id is paired as the `newTaskId`. Otherwise the old
   * task is considered dropped and `newTaskId` stays empty so the
   * renderer can draw a dangling "retired" marker.
   */
  supersedesMap(planId: string): Map<string, SupersessionLink> {
    const out = new Map<string, SupersessionLink>();
    const arr = this.revisions.get(planId);
    if (!arr || arr.length < 2) return out;
    // Track every task id ever seen, so we can distinguish genuinely new
    // tasks (candidates for "newTaskId" pairing) from re-surfaced ids.
    const everSeen = new Set<string>();
    for (const t of arr[0].plan.tasks) everSeen.add(t.id);
    for (let i = 1; i < arr.length; i++) {
      const prev = arr[i - 1].plan;
      const next = arr[i].plan;
      const nextIds = new Set(next.tasks.map((t) => t.id));
      const dropped: string[] = [];
      for (const t of prev.tasks) {
        if (!nextIds.has(t.id)) dropped.push(t.id);
      }
      const trulyNew: string[] = [];
      for (const t of next.tasks) {
        if (!everSeen.has(t.id)) trulyNew.push(t.id);
      }
      // Pair one-for-one when possible; if counts mismatch, pair prefix
      // and leave the remainder as dropped-without-successor.
      const pairs = Math.min(dropped.length, trulyNew.length);
      const rec = arr[i];
      for (let k = 0; k < dropped.length; k++) {
        out.set(dropped[k], {
          oldTaskId: dropped[k],
          newTaskId: k < pairs ? trulyNew[k] : '',
          revision: rec.revision,
          kind: rec.kind,
          reason: rec.reason,
          triggerEventId: rec.triggerEventId,
        });
      }
      for (const id of nextIds) everSeen.add(id);
    }
    return out;
  }

  /**
   * List every plan id known to the registry. Order is insertion (first
   * append wins); callers sort by `createdAtMs` if needed.
   */
  planIds(): string[] {
    return Array.from(this.revisions.keys());
  }

  /** Wipe everything. Called from SessionStore.clear(). */
  clear(): void {
    if (this.revisions.size === 0) return;
    this.revisions.clear();
    this.emit();
  }

  /** Subscribe to any change. Returns an unsubscribe fn. */
  subscribe(fn: () => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) fn();
  }
}

const EMPTY_REVISIONS: PlanRevisionRecord[] = Object.freeze(
  [],
) as unknown as PlanRevisionRecord[];
