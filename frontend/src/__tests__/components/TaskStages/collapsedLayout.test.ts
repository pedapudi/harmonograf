import { describe, expect, it } from 'vitest';
import {
  collapseCumulativePlan,
  filterCollapsedAtRevision,
} from '../../../components/TaskStages/collapsedLayout';
import type { Task, TaskEdge } from '../../../gantt/types';
import type {
  CumulativePlan,
  CumulativeTaskMeta,
  SupersessionLink,
} from '../../../state/planHistoryStore';

// ── Fixture helpers ────────────────────────────────────────────────────

function makeTask(id: string, title?: string): Task {
  return {
    id,
    title: title ?? `Task ${id}`,
    description: '',
    assigneeAgentId: 'agent-a',
    status: 'PENDING',
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
  };
}

interface PlanFixtureInput {
  id?: string;
  /** [task, revisionIntroduced] pairs in cumulative-task-list order. */
  tasks: Array<[Task, number]>;
  edges?: TaskEdge[];
}

function makeCumulativePlan(input: PlanFixtureInput): CumulativePlan {
  const taskRevisionMeta = new Map<string, CumulativeTaskMeta>();
  for (const [t, rev] of input.tasks) {
    taskRevisionMeta.set(t.id, {
      introducedInRevision: rev,
      lastModifiedInRevision: rev,
      isSuperseded: false,
    });
  }
  return {
    id: input.id ?? 'plan-1',
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks: input.tasks.map(([t]) => t),
    edges: input.edges ?? [],
    revisionReason: '',
    revisionKind: '',
    revisionSeverity: '',
    revisionIndex: 0,
    triggerEventId: '',
    taskRevisionMeta,
  };
}

function makeSupersedesMap(
  entries: Array<[string, string, number?]>,
): Map<string, SupersessionLink> {
  // [oldId, newId, revision?]. newId="" represents a dangling drop.
  const out = new Map<string, SupersessionLink>();
  for (const [oldId, newId, rev] of entries) {
    out.set(oldId, {
      oldTaskId: oldId,
      newTaskId: newId,
      revision: rev ?? 1,
      kind: '',
      reason: '',
      triggerEventId: '',
    });
  }
  return out;
}

// ── collapseCumulativePlan ─────────────────────────────────────────────

describe('collapseCumulativePlan', () => {
  it('returns empty chains/edges for an empty plan', () => {
    const plan = makeCumulativePlan({ tasks: [] });
    const result = collapseCumulativePlan(plan, new Map());
    expect(result.chains).toEqual([]);
    expect(result.edges).toEqual([]);
    expect(result.chainByTaskId.size).toBe(0);
    expect(result.planId).toBe('plan-1');
  });

  it('emits one singleton chain per unrelated task', () => {
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('a'), 0],
        [makeTask('b'), 0],
        [makeTask('c'), 1],
      ],
    });
    const result = collapseCumulativePlan(plan, new Map());
    expect(result.chains).toHaveLength(3);
    for (const chain of result.chains) {
      expect(chain.members).toHaveLength(1);
      expect(chain.canonical).toBe(chain.members[0]);
    }
    expect(result.chains.map((c) => c.canonical.id)).toEqual(['a', 'b', 'c']);
    expect(result.chains[2].revisions).toEqual([1]);
  });

  it('collapses a simple 2-member chain a→b', () => {
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('a'), 0],
        [makeTask('b'), 1],
      ],
    });
    const supersedes = makeSupersedesMap([['a', 'b', 1]]);
    const result = collapseCumulativePlan(plan, supersedes);
    expect(result.chains).toHaveLength(1);
    const chain = result.chains[0];
    expect(chain.members.map((m) => m.id)).toEqual(['a', 'b']);
    expect(chain.canonical.id).toBe('b');
    expect(chain.revisions).toEqual([0, 1]);
    expect(result.chainByTaskId.get('a')).toBe(chain);
    expect(result.chainByTaskId.get('b')).toBe(chain);
  });

  it('collapses a 3-member chain a→b→c', () => {
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('a'), 0],
        [makeTask('b'), 1],
        [makeTask('c'), 2],
      ],
    });
    const supersedes = makeSupersedesMap([
      ['a', 'b', 1],
      ['b', 'c', 2],
    ]);
    const result = collapseCumulativePlan(plan, supersedes);
    expect(result.chains).toHaveLength(1);
    const chain = result.chains[0];
    expect(chain.members.map((m) => m.id)).toEqual(['a', 'b', 'c']);
    expect(chain.canonical.id).toBe('c');
    expect(chain.revisions).toEqual([0, 1, 2]);
  });

  it('handles mixed plans with singletons + a chain', () => {
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('x'), 0],
        [makeTask('a'), 0],
        [makeTask('b'), 1],
        [makeTask('c'), 2],
        [makeTask('y'), 0],
      ],
    });
    const supersedes = makeSupersedesMap([
      ['a', 'b', 1],
      ['b', 'c', 2],
    ]);
    const result = collapseCumulativePlan(plan, supersedes);
    expect(result.chains).toHaveLength(3);
    const ids = result.chains.map((c) => c.canonical.id);
    expect(ids).toContain('x');
    expect(ids).toContain('y');
    expect(ids).toContain('c');
    const abcChain = result.chains.find((c) => c.canonical.id === 'c')!;
    expect(abcChain.members.map((m) => m.id)).toEqual(['a', 'b', 'c']);
  });

  it('rewrites edges whose target is a superseded member to the canonical', () => {
    // A → B, and B is superseded by B'. Result: A → B'.
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('A'), 0],
        [makeTask('B'), 0],
        [makeTask('Bp'), 1],
      ],
      edges: [{ fromTaskId: 'A', toTaskId: 'B' }],
    });
    const supersedes = makeSupersedesMap([['B', 'Bp', 1]]);
    const result = collapseCumulativePlan(plan, supersedes);
    expect(result.edges).toEqual([{ fromTaskId: 'A', toTaskId: 'Bp' }]);
  });

  it('drops self-edges where both endpoints collapse to the same chain', () => {
    // a → b where a and b are members of the same chain.
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('a'), 0],
        [makeTask('b'), 1],
      ],
      edges: [{ fromTaskId: 'a', toTaskId: 'b' }],
    });
    const supersedes = makeSupersedesMap([['a', 'b', 1]]);
    const result = collapseCumulativePlan(plan, supersedes);
    expect(result.edges).toEqual([]);
  });

  it('dedups edges that target different members of the same chain', () => {
    // A → B, A → B' (B superseded by B'). Both rewrite to A → B' — keep
    // one.
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('A'), 0],
        [makeTask('B'), 0],
        [makeTask('Bp'), 1],
      ],
      edges: [
        { fromTaskId: 'A', toTaskId: 'B' },
        { fromTaskId: 'A', toTaskId: 'Bp' },
      ],
    });
    const supersedes = makeSupersedesMap([['B', 'Bp', 1]]);
    const result = collapseCumulativePlan(plan, supersedes);
    expect(result.edges).toEqual([{ fromTaskId: 'A', toTaskId: 'Bp' }]);
  });

  it('treats a dangling drop (newTaskId === "") as a singleton chain', () => {
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('x'), 0],
        [makeTask('y'), 0],
      ],
    });
    const supersedes = makeSupersedesMap([['x', '', 1]]);
    const result = collapseCumulativePlan(plan, supersedes);
    expect(result.chains).toHaveLength(2);
    for (const chain of result.chains) {
      expect(chain.members).toHaveLength(1);
    }
  });

  it('ignores orphan supersedes links (ids absent from cumulative.tasks)', () => {
    // Link references "ghost" which isn't in the plan — treat as if the
    // link didn't exist; "a" stays a singleton chain.
    const plan = makeCumulativePlan({
      tasks: [[makeTask('a'), 0]],
    });
    const supersedes = makeSupersedesMap([['a', 'ghost', 1]]);
    const result = collapseCumulativePlan(plan, supersedes);
    expect(result.chains).toHaveLength(1);
    expect(result.chains[0].members.map((m) => m.id)).toEqual(['a']);
  });
});

// ── filterCollapsedAtRevision ──────────────────────────────────────────

describe('filterCollapsedAtRevision', () => {
  it('revision=null leaves everything visible with no muting', () => {
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('a'), 0],
        [makeTask('b'), 1],
      ],
    });
    const supersedes = makeSupersedesMap([['a', 'b', 1]]);
    const collapsed = collapseCumulativePlan(plan, supersedes);
    const filtered = filterCollapsedAtRevision(collapsed, null);
    expect(filtered.chains).toBe(collapsed.chains);
    expect(filtered.edges).toBe(collapsed.edges);
    expect(filtered.hiddenChainIds.size).toBe(0);
    expect(filtered.mutedChainIds.size).toBe(0);
  });

  it('renders a chain entirely introduced in rev 0 as visible and unmuted at rev 0', () => {
    const plan = makeCumulativePlan({
      tasks: [[makeTask('a'), 0]],
    });
    const collapsed = collapseCumulativePlan(plan, new Map());
    const filtered = filterCollapsedAtRevision(collapsed, 0);
    expect(filtered.hiddenChainIds.size).toBe(0);
    expect(filtered.mutedChainIds.size).toBe(0);
  });

  it('mutes a chain whose root exists at rev 0 but canonical was introduced later', () => {
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('a'), 0],
        [makeTask('b'), 1],
      ],
    });
    const supersedes = makeSupersedesMap([['a', 'b', 1]]);
    const collapsed = collapseCumulativePlan(plan, supersedes);
    const filtered = filterCollapsedAtRevision(collapsed, 0);
    // Chain canonical is 'b' (introduced in rev 1); root 'a' is rev 0.
    expect(filtered.hiddenChainIds.size).toBe(0);
    expect(filtered.mutedChainIds.has('b')).toBe(true);
  });

  it('hides a chain whose members were all introduced after the pinned revision', () => {
    const plan = makeCumulativePlan({
      tasks: [
        [makeTask('a'), 0],
        [makeTask('new'), 1],
      ],
    });
    const collapsed = collapseCumulativePlan(plan, new Map());
    const filtered = filterCollapsedAtRevision(collapsed, 0);
    expect(filtered.hiddenChainIds.has('new')).toBe(true);
    expect(filtered.hiddenChainIds.has('a')).toBe(false);
    expect(filtered.mutedChainIds.size).toBe(0);
  });
});
