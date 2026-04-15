import { describe, expect, it } from 'vitest';
import { computePlanDiff, TaskRegistry } from '../../gantt/index';
import type { Task, TaskPlan } from '../../gantt/types';

function mkTask(id: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    title: `task ${id}`,
    description: '',
    assigneeAgentId: 'agent-a',
    status: 'PENDING',
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    ...overrides,
  };
}

function mkPlan(id: string, overrides: Partial<TaskPlan> = {}): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}`,
    plannerAgentId: 'planner',
    createdAtMs: 0,
    summary: '',
    tasks: [mkTask('t1'), mkTask('t2')],
    edges: [{ fromTaskId: 't1', toTaskId: 't2' }],
    revisionReason: '',
    ...overrides,
  };
}

describe('computePlanDiff', () => {
  it('flags every next-plan task as added when there is no previous plan', () => {
    const next = mkPlan('p1');
    const diff = computePlanDiff(undefined, next);
    expect(diff.added.map((t) => t.id)).toEqual(['t1', 't2']);
    expect(diff.removed).toEqual([]);
    expect(diff.modified).toEqual([]);
    // undefined prev → edges "changed" because there were none before.
    expect(diff.edgesChanged).toBe(true);
  });

  it('returns empty diff when plans are identical', () => {
    const a = mkPlan('p1');
    const b = mkPlan('p1');
    const diff = computePlanDiff(a, b);
    expect(diff.added).toEqual([]);
    expect(diff.removed).toEqual([]);
    expect(diff.modified).toEqual([]);
    expect(diff.edgesChanged).toBe(false);
  });

  it('detects added, removed, and modified tasks together', () => {
    const prev = mkPlan('p1', {
      tasks: [
        mkTask('keep', { title: 'K', status: 'PENDING' }),
        mkTask('gone'),
        mkTask('mut', { assigneeAgentId: 'agent-a', status: 'PENDING' }),
      ],
      edges: [],
    });
    const next = mkPlan('p1', {
      tasks: [
        mkTask('keep', { title: 'K', status: 'PENDING' }),
        mkTask('mut', { assigneeAgentId: 'agent-b', status: 'RUNNING' }),
        mkTask('brand-new'),
      ],
      edges: [],
    });
    const diff = computePlanDiff(prev, next);
    expect(diff.added.map((t) => t.id)).toEqual(['brand-new']);
    expect(diff.removed.map((r) => r.id)).toEqual(['gone']);
    expect(diff.modified).toHaveLength(1);
    expect(diff.modified[0].id).toBe('mut');
    expect(diff.modified[0].changes.sort()).toEqual(['assignee', 'status']);
    expect(diff.edgesChanged).toBe(false);
  });

  it('detects title and description changes independently', () => {
    const prev = mkPlan('p1', {
      tasks: [
        mkTask('t1', { title: 'old title', description: 'old desc' }),
      ],
      edges: [],
    });
    const next = mkPlan('p1', {
      tasks: [
        mkTask('t1', { title: 'new title', description: 'new desc' }),
      ],
      edges: [],
    });
    const diff = computePlanDiff(prev, next);
    expect(diff.modified[0].changes.sort()).toEqual(['description', 'title']);
  });

  it('removed entries carry the previous title so UI can render them', () => {
    const prev = mkPlan('p1', {
      tasks: [mkTask('t1', { title: 'Step One' })],
      edges: [],
    });
    const next = mkPlan('p1', { tasks: [], edges: [] });
    const diff = computePlanDiff(prev, next);
    expect(diff.removed).toEqual([{ id: 't1', title: 'Step One' }]);
  });

  it('edgesChanged is order-insensitive', () => {
    const prev = mkPlan('p1', {
      edges: [
        { fromTaskId: 't1', toTaskId: 't2' },
        { fromTaskId: 't2', toTaskId: 't3' },
      ],
    });
    const next = mkPlan('p1', {
      edges: [
        { fromTaskId: 't2', toTaskId: 't3' },
        { fromTaskId: 't1', toTaskId: 't2' },
      ],
    });
    expect(computePlanDiff(prev, next).edgesChanged).toBe(false);
  });

  it('edgesChanged flips when an edge is added or removed', () => {
    const prev = mkPlan('p1', { edges: [{ fromTaskId: 't1', toTaskId: 't2' }] });
    const next = mkPlan('p1', {
      edges: [
        { fromTaskId: 't1', toTaskId: 't2' },
        { fromTaskId: 't2', toTaskId: 't1' },
      ],
    });
    expect(computePlanDiff(prev, next).edgesChanged).toBe(true);
  });
});

describe('TaskRegistry records diffs on revision', () => {
  it('first revision with no prior plan yields an all-added diff', () => {
    const reg = new TaskRegistry();
    reg.upsertPlan(mkPlan('p1', { revisionReason: 'initial refine' }));
    const revisions = reg.revisionsForPlan('p1');
    expect(revisions).toHaveLength(1);
    expect(revisions[0].diff.added).toHaveLength(2);
    expect(revisions[0].diff.removed).toEqual([]);
  });

  it('subsequent revision diffs against the previous plan snapshot', () => {
    const reg = new TaskRegistry();
    reg.upsertPlan(
      mkPlan('p1', {
        tasks: [mkTask('t1'), mkTask('t2')],
        revisionReason: 'first',
      }),
    );
    reg.upsertPlan(
      mkPlan('p1', {
        tasks: [
          mkTask('t1', { status: 'COMPLETED' }),
          mkTask('t3'),
        ],
        revisionReason: 'second',
      }),
    );
    const revisions = reg.revisionsForPlan('p1');
    expect(revisions).toHaveLength(2);
    const latest = revisions[1];
    expect(latest.reason).toBe('second');
    expect(latest.diff.added.map((t) => t.id)).toEqual(['t3']);
    expect(latest.diff.removed.map((r) => r.id)).toEqual(['t2']);
    expect(latest.diff.modified).toHaveLength(1);
    expect(latest.diff.modified[0].changes).toEqual(['status']);
  });

  it('diff is computed against sibling when invocationSpanId dedup kicks in', () => {
    const reg = new TaskRegistry();
    reg.upsertPlan(
      mkPlan('p1', {
        invocationSpanId: 'inv-shared',
        tasks: [mkTask('t1')],
        revisionReason: 'first',
      }),
    );
    reg.upsertPlan(
      mkPlan('p2', {
        invocationSpanId: 'inv-shared',
        tasks: [mkTask('t1'), mkTask('t2')],
        revisionReason: 'refined via sibling',
      }),
    );
    const revisions = reg.revisionsForPlan('p2');
    expect(revisions).toHaveLength(1);
    expect(revisions[0].diff.added.map((t) => t.id)).toEqual(['t2']);
    expect(revisions[0].diff.removed).toEqual([]);
  });
});
