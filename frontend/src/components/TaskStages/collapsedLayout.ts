// Supersedes-aware DAG collapse for the cumulative-plan renderer.
//
// The cumulative plan (planHistoryStore.cumulativePlan) unions every task id
// ever seen across every revision of a plan. Goldfive's refine workflow mints
// a fresh task id per replacement, so a plan that has been revised 5 times
// accumulates 5 "Research solar panel" tasks at stage 0, 5 "Create solar
// panel pr" tasks at stage 1, etc. When rendered as distinct positional
// nodes the result is a vertical stack of near-duplicate cards that obscure
// the actual plan shape.
//
// This module collapses supersedes chains (a → b → c) into a single
// positional slot represented by a `TaskRevisionChain`. The chain's latest
// member is the canonical representative that the layout positions; the
// superseded predecessors become per-slot metadata that the chrome can
// surface as a revision history badge.
//
// Pure data transform — no React, no layout math. Consumed by
// TaskStagesGraph (via the BAG integration change) to build the collapsed
// node set + edge list before running existing topological positioning.

import type { Task, TaskEdge } from '../../gantt/types';
import type {
  CumulativePlan,
  SupersessionLink,
} from '../../state/planHistoryStore';

export interface TaskRevisionChain {
  /** The chain's canonical representative: the latest non-superseded
   *  member. The node that the layout positions and the UI foregrounds. */
  canonical: Task;
  /** All chain members ordered oldest → newest. Always non-empty;
   *  canonical is always `members[members.length - 1]`. Length 1 when
   *  the task has no supersedes relationships. */
  members: Task[];
  /** Revision in which each member was introduced (index-aligned with
   *  `members`). Length equals members.length. */
  revisions: number[];
}

export interface CollapsedCumulativePlan {
  planId: string;
  /** One chain per logical task slot. */
  chains: TaskRevisionChain[];
  /** Edges rewritten so every endpoint is a chain CANONICAL id. Edges
   *  whose original endpoint was a non-canonical (superseded) member
   *  are redirected to that chain's canonical. Self-edges (a chain's
   *  members reaching into themselves via an intermediate non-canonical
   *  hop) are dropped. Duplicate (from, to) pairs are dedup'd. */
  edges: TaskEdge[];
  /** Lookup: any member id → its chain. */
  chainByTaskId: Map<string, TaskRevisionChain>;
}

/** Collapse a cumulative plan's tasks + edges into supersedes-equivalence
 *  classes keyed by canonical representative.
 *
 *  Uses `supersedesMap` (keyed by oldTaskId → { newTaskId }) as the
 *  authoritative source of chain membership. A task with no entry in
 *  the map is its own singleton chain. Chains form transitively:
 *  a→b, b→c produces one chain {a, b, c}. Orphan chains (link
 *  references a task not present in the cumulative tasks) resolve
 *  as if the missing link didn't exist.
 *
 *  Assumptions about `CumulativePlan.taskRevisionMeta`: every task id
 *  has a `introducedInRevision` value; canonicals are chosen as the
 *  task with the highest introducedInRevision within a chain that is
 *  NOT an oldTaskId in the supersedes map.
 */
export function collapseCumulativePlan(
  cumulative: CumulativePlan,
  supersedesMap: Map<string, SupersessionLink>,
): CollapsedCumulativePlan {
  // Tasks present in this cumulative plan, indexed by id for O(1) lookup
  // while walking supersedes links.
  const tasksById = new Map<string, Task>();
  for (const t of cumulative.tasks) tasksById.set(t.id, t);

  // Forward edges of the supersedes relation: oldTaskId -> newTaskId.
  // Drop entries with an empty newTaskId (dangling drop without a
  // replacement — the old task becomes its own singleton chain) and
  // orphan links whose endpoints are not in the cumulative task set.
  const successor = new Map<string, string>();
  for (const [oldId, link] of supersedesMap) {
    const newId = link.newTaskId;
    if (!newId) continue;
    if (!tasksById.has(oldId) || !tasksById.has(newId)) continue;
    successor.set(oldId, newId);
  }

  // Inverse edges so we can rewind to a chain's root.
  const predecessor = new Map<string, string>();
  for (const [oldId, newId] of successor) {
    // Guard against a (malformed) fork where two old tasks point to the
    // same new task — keep the first predecessor and ignore subsequent
    // ones so the walk stays deterministic.
    if (!predecessor.has(newId)) predecessor.set(newId, oldId);
  }

  const chains: TaskRevisionChain[] = [];
  const chainByTaskId = new Map<string, TaskRevisionChain>();

  // Walk tasks in the order they appear in `cumulative.tasks` to keep
  // chain discovery deterministic. Cache chain membership so forward +
  // backward walks from different members of the same chain don't redo
  // work.
  for (const task of cumulative.tasks) {
    if (chainByTaskId.has(task.id)) continue;

    // Rewind to the chain's oldest member (no predecessor). Follow
    // `predecessor` backwards, guarding against cycles (shouldn't occur
    // given append-only supersedes semantics, but defensive).
    const seenInRewind = new Set<string>();
    let rootId = task.id;
    while (predecessor.has(rootId)) {
      if (seenInRewind.has(rootId)) break; // cycle guard
      seenInRewind.add(rootId);
      rootId = predecessor.get(rootId)!;
    }

    // Walk forward from the root collecting chain members.
    const members: Task[] = [];
    const revisions: number[] = [];
    const seenInWalk = new Set<string>();
    let cursor: string | undefined = rootId;
    while (cursor !== undefined) {
      if (seenInWalk.has(cursor)) break; // cycle guard
      seenInWalk.add(cursor);
      const memberTask = tasksById.get(cursor);
      if (!memberTask) break; // orphan — stop walk gracefully
      members.push(memberTask);
      const meta = cumulative.taskRevisionMeta.get(cursor);
      revisions.push(meta ? meta.introducedInRevision : 0);
      cursor = successor.get(cursor);
    }

    // Defensive: `task` might not itself be reachable from `rootId` if
    // the supersedes graph is malformed (task sits on a disconnected
    // branch that shares an id with something we rewound into). In that
    // case fall back to a singleton chain so we still render the task.
    if (members.length === 0 || !seenInWalk.has(task.id)) {
      const meta = cumulative.taskRevisionMeta.get(task.id);
      const chain: TaskRevisionChain = {
        canonical: task,
        members: [task],
        revisions: [meta ? meta.introducedInRevision : 0],
      };
      chains.push(chain);
      chainByTaskId.set(task.id, chain);
      continue;
    }

    const chain: TaskRevisionChain = {
      canonical: members[members.length - 1],
      members,
      revisions,
    };
    chains.push(chain);
    for (const m of members) chainByTaskId.set(m.id, chain);
  }

  // Rewrite edges to anchor on canonicals; drop self-edges and
  // duplicates. Edges whose endpoint is unknown to any chain (foreign
  // id leaked into the edge list) are dropped — the renderer has no
  // positional slot to attach them to.
  const edgeKeys = new Set<string>();
  const edges: TaskEdge[] = [];
  for (const edge of cumulative.edges) {
    const fromChain = chainByTaskId.get(edge.fromTaskId);
    const toChain = chainByTaskId.get(edge.toTaskId);
    if (!fromChain || !toChain) continue;
    const canonicalFrom = fromChain.canonical.id;
    const canonicalTo = toChain.canonical.id;
    if (canonicalFrom === canonicalTo) continue; // self-edge dropped
    const key = `${canonicalFrom}→${canonicalTo}`;
    if (edgeKeys.has(key)) continue;
    edgeKeys.add(key);
    edges.push({ fromTaskId: canonicalFrom, toTaskId: canonicalTo });
  }

  return {
    planId: cumulative.id,
    chains,
    edges,
    chainByTaskId,
  };
}

/** Apply a "pinned at revision" filter to a collapsed plan.
 *
 *  - revision === null: no filter; all chains visible, no muting.
 *  - revision === N: chains whose canonical was introduced in a
 *    revision ≤ N render normally. Chains whose canonical was
 *    introduced in revision > N are returned in `hiddenChainIds`
 *    (so the caller can omit them from layout) and/or
 *    `mutedChainIds` (so the caller can dim them).
 *
 *  Trade-off (first-pass): if a chain's root was introduced on/before
 *  N but a later member (still on/before N) is the canonical, we
 *  render it normally. If the chain's root was introduced on/before N
 *  but the canonical is later than N, we MUTE but still render the
 *  full chain with its latest-ever canonical — the caller dims it so
 *  the operator can see "this slot exists but the displayed face is
 *  from a future revision". A later pass may replace the canonical
 *  with the newest member ≤ N so the displayed face is period-
 *  accurate; that requires constructing a new TaskRevisionChain which
 *  changes the return contract, so INT will land that refinement.
 */
export function filterCollapsedAtRevision(
  collapsed: CollapsedCumulativePlan,
  revision: number | null,
): {
  chains: readonly TaskRevisionChain[];
  edges: readonly TaskEdge[];
  hiddenChainIds: ReadonlySet<string>;
  mutedChainIds: ReadonlySet<string>;
} {
  if (revision === null) {
    return {
      chains: collapsed.chains,
      edges: collapsed.edges,
      hiddenChainIds: new Set(),
      mutedChainIds: new Set(),
    };
  }

  const hiddenChainIds = new Set<string>();
  const mutedChainIds = new Set<string>();
  for (const chain of collapsed.chains) {
    let firstRev = Number.POSITIVE_INFINITY;
    let latestRev = Number.NEGATIVE_INFINITY;
    for (const r of chain.revisions) {
      if (r < firstRev) firstRev = r;
      if (r > latestRev) latestRev = r;
    }
    if (firstRev > revision) {
      // Chain not yet born at this rev.
      hiddenChainIds.add(chain.canonical.id);
    } else if (latestRev > revision) {
      // Root existed, but the displayed canonical is from a future rev.
      // Render muted so the operator can tell this slot's face is newer
      // than the pinned revision.
      mutedChainIds.add(chain.canonical.id);
    }
  }

  return {
    chains: collapsed.chains,
    edges: collapsed.edges,
    hiddenChainIds,
    mutedChainIds,
  };
}
