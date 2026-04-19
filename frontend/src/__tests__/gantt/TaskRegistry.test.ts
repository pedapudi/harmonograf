import { beforeEach, describe, expect, it, vi } from 'vitest';
import { TaskRegistry } from '../../gantt/index';
import type { Task, TaskPlan } from '../../gantt/types';

function makeTask(id: string, overrides: Partial<Task> = {}): Task {
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

function makePlan(id: string, overrides: Partial<TaskPlan> = {}): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}`,
    plannerAgentId: 'planner',
    createdAtMs: 0,
    summary: `plan ${id}`,
    tasks: [makeTask('t1'), makeTask('t2')],
    edges: [{ fromTaskId: 't1', toTaskId: 't2' }],
    revisionReason: '',
    ...overrides,
  };
}

describe('TaskRegistry', () => {
  let reg: TaskRegistry;
  beforeEach(() => {
    reg = new TaskRegistry();
  });

  it('upsertPlan stores a fresh plan', () => {
    const p = makePlan('p1');
    reg.upsertPlan(p);
    expect(reg.size).toBe(1);
    expect(reg.getPlan('p1')).toBe(p);
    expect(reg.listPlans()).toHaveLength(1);
  });

  it('re-upserting replaces tasks+edges by object identity (not merge)', () => {
    reg.upsertPlan(makePlan('p1'));
    const replacement = makePlan('p1', {
      tasks: [makeTask('t9')],
      edges: [],
    });
    reg.upsertPlan(replacement);
    expect(reg.size).toBe(1);
    expect(reg.getPlan('p1')).toBe(replacement);
    expect(reg.getPlan('p1')!.tasks).toHaveLength(1);
    expect(reg.getPlan('p1')!.tasks[0].id).toBe('t9');
  });

  it('de-dups by invocationSpanId across distinct plan_ids', () => {
    reg.upsertPlan(makePlan('p1', { invocationSpanId: 'inv-shared' }));
    reg.upsertPlan(makePlan('p2', { invocationSpanId: 'inv-shared' }));
    expect(reg.size).toBe(1);
    expect(reg.getPlan('p1')).toBeUndefined();
    expect(reg.getPlan('p2')).toBeDefined();
  });

  it('updateTaskStatus mutates in place and emits', () => {
    reg.upsertPlan(makePlan('p1'));
    const fn = vi.fn();
    reg.subscribe(fn);
    reg.updateTaskStatus('p1', 't1', 'RUNNING', 'span-xyz');
    const t = reg.getPlan('p1')!.tasks.find((x) => x.id === 't1')!;
    expect(t.status).toBe('RUNNING');
    expect(t.boundSpanId).toBe('span-xyz');
    expect(fn).toHaveBeenCalled();
  });

  it('updateTaskStatus on missing plan/task is a no-op', () => {
    reg.upsertPlan(makePlan('p1'));
    expect(() => reg.updateTaskStatus('missing', 't1', 'RUNNING', '')).not.toThrow();
    expect(() => reg.updateTaskStatus('p1', 'missing', 'RUNNING', '')).not.toThrow();
  });

  it('updateTaskStatusByTaskId finds the task across plans and no-ops on unknown ids', () => {
    reg.upsertPlan(makePlan('p1', { invocationSpanId: 'inv-p1' }));
    reg.upsertPlan(
      makePlan('p2', {
        invocationSpanId: 'inv-p2',
        tasks: [makeTask('tX'), makeTask('tY')],
      }),
    );
    const fn = vi.fn();
    reg.subscribe(fn);

    reg.updateTaskStatusByTaskId('tX', 'RUNNING', 'span-xy');
    const tX = reg.getPlan('p2')!.tasks.find((t) => t.id === 'tX')!;
    expect(tX.status).toBe('RUNNING');
    expect(tX.boundSpanId).toBe('span-xy');
    expect(fn).toHaveBeenCalledTimes(1);

    expect(() =>
      reg.updateTaskStatusByTaskId('nonexistent', 'BLOCKED'),
    ).not.toThrow();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('listPlans returns plans sorted by createdAtMs', () => {
    reg.upsertPlan(makePlan('p2', { createdAtMs: 200, invocationSpanId: 'inv-p2' }));
    reg.upsertPlan(makePlan('p1', { createdAtMs: 100, invocationSpanId: 'inv-p1' }));
    reg.upsertPlan(makePlan('p3', { createdAtMs: 300, invocationSpanId: 'inv-p3' }));
    const ids = reg.listPlans().map((p) => p.id);
    expect(ids).toEqual(['p1', 'p2', 'p3']);
  });

  it('tasksForAgent filters by assignee across plans', () => {
    reg.upsertPlan(
      makePlan('p1', {
        invocationSpanId: 'inv-p1',
        tasks: [
          makeTask('t1', { assigneeAgentId: 'a' }),
          makeTask('t2', { assigneeAgentId: 'b' }),
        ],
      }),
    );
    reg.upsertPlan(
      makePlan('p2', {
        invocationSpanId: 'inv-p2',
        tasks: [makeTask('t3', { assigneeAgentId: 'a' })],
      }),
    );
    const aTasks = reg.tasksForAgent('a');
    expect(aTasks.map((t) => t.id).sort()).toEqual(['t1', 't3']);
    expect(reg.tasksForAgent('b').map((t) => t.id)).toEqual(['t2']);
    expect(reg.tasksForAgent('none')).toEqual([]);
  });

  it('findPlanForTask locates the right plan', () => {
    reg.upsertPlan(makePlan('p1', { invocationSpanId: 'inv-p1' }));
    reg.upsertPlan(
      makePlan('p2', {
        invocationSpanId: 'inv-p2',
        tasks: [makeTask('deep')],
      }),
    );
    const hit = reg.findPlanForTask('deep');
    expect(hit?.plan.id).toBe('p2');
    expect(hit?.task.id).toBe('deep');
    expect(reg.findPlanForTask('missing')).toBeUndefined();
  });

  it('revision tracking appends on new reason, idempotent on same reason', () => {
    reg.upsertPlan(makePlan('p1', { revisionReason: 'first' }));
    reg.upsertPlan(makePlan('p1', { revisionReason: 'first' }));
    expect(reg.revisionsForPlan('p1')).toHaveLength(1);
    reg.upsertPlan(makePlan('p1', { revisionReason: 'second' }));
    const rs = reg.revisionsForPlan('p1');
    expect(rs).toHaveLength(2);
    expect(rs[0].reason).toBe('first');
    expect(rs[1].reason).toBe('second');
  });

  it('revisionsForPlan caps at 20 entries', () => {
    reg.upsertPlan(makePlan('p1'));
    for (let i = 0; i < 25; i++) {
      reg.upsertPlan(makePlan('p1', { revisionReason: `r${i}` }));
    }
    const rs = reg.revisionsForPlan('p1');
    expect(rs.length).toBeLessThanOrEqual(20);
    expect(rs[rs.length - 1].reason).toBe('r24');
  });

  it('clear wipes plans and revision history', () => {
    reg.upsertPlan(makePlan('p1', { revisionReason: 'x' }));
    reg.clear();
    expect(reg.size).toBe(0);
    expect(reg.listPlans()).toHaveLength(0);
    expect(reg.revisionsForPlan('p1')).toEqual([]);
  });

  it('subscribe returns an unsubscribe that stops firing', () => {
    const fn = vi.fn();
    const off = reg.subscribe(fn);
    reg.upsertPlan(makePlan('p1'));
    expect(fn).toHaveBeenCalledTimes(1);
    off();
    reg.upsertPlan(makePlan('p2', { invocationSpanId: 'inv-p2' }));
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
