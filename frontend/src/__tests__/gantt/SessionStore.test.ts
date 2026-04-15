import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Task, TaskPlan, TaskStatus } from '../../gantt/types';

function plan(id: string, tasks: Task[]): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}`,
    plannerAgentId: 'planner',
    createdAtMs: 0,
    summary: '',
    tasks,
    edges: [],
    revisionReason: '',
  };
}

function task(id: string, status: TaskStatus): Task {
  return {
    id,
    title: id,
    description: '',
    assigneeAgentId: 'a',
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
  };
}

describe('SessionStore.getCurrentTask', () => {
  let store: SessionStore;
  beforeEach(() => {
    store = new SessionStore();
  });

  it('returns null when no plans are loaded', () => {
    expect(store.getCurrentTask()).toBeNull();
  });

  it('prefers a RUNNING task over any terminal task', () => {
    store.tasks.upsertPlan(
      plan('p1', [task('a', 'COMPLETED'), task('b', 'RUNNING'), task('c', 'FAILED')]),
    );
    expect(store.getCurrentTask()?.task.id).toBe('b');
  });

  it('falls back to last COMPLETED when none RUNNING', () => {
    store.tasks.upsertPlan(
      plan('p1', [task('a', 'COMPLETED'), task('b', 'PENDING')]),
    );
    expect(store.getCurrentTask()?.task.id).toBe('a');
  });

  it('accepts FAILED/CANCELLED as fallback terminal states', () => {
    store.tasks.upsertPlan(plan('p1', [task('a', 'FAILED')]));
    expect(store.getCurrentTask()?.task.status).toBe('FAILED');

    const s2 = new SessionStore();
    s2.tasks.upsertPlan(plan('p1', [task('a', 'CANCELLED')]));
    expect(s2.getCurrentTask()?.task.status).toBe('CANCELLED');
  });

  it('returns null when every task is still PENDING', () => {
    store.tasks.upsertPlan(plan('p1', [task('a', 'PENDING'), task('b', 'PENDING')]));
    expect(store.getCurrentTask()).toBeNull();
  });
});

describe('SessionStore.clear', () => {
  it('wipes agents, spans, tasks, and nowMs', () => {
    const store = new SessionStore();
    store.nowMs = 5000;
    store.agents.upsert({
      id: 'a',
      name: 'a',
      framework: 'ADK',
      capabilities: [],
      status: 'CONNECTED',
      connectedAtMs: 0,
      currentActivity: '',
      stuck: false,
      taskReport: '',
      taskReportAt: 0,
      metadata: {},
    });
    store.tasks.upsertPlan(plan('p1', [task('a', 'RUNNING')]));
    store.clear();
    expect(store.agents.size).toBe(0);
    expect(store.tasks.size).toBe(0);
    expect(store.nowMs).toBe(0);
  });
});

describe('registries emit to subscribers on mutation', () => {
  it('AgentRegistry.upsert fires subscribe', () => {
    const store = new SessionStore();
    const fn = vi.fn();
    store.agents.subscribe(fn);
    store.agents.upsert({
      id: 'a',
      name: 'a',
      framework: 'ADK',
      capabilities: [],
      status: 'CONNECTED',
      connectedAtMs: 0,
      currentActivity: '',
      stuck: false,
      taskReport: '',
      taskReportAt: 0,
      metadata: {},
    });
    expect(fn).toHaveBeenCalled();
  });

  it('TaskRegistry.updateTaskStatus fires subscribe', () => {
    const store = new SessionStore();
    store.tasks.upsertPlan(plan('p1', [task('a', 'PENDING')]));
    const fn = vi.fn();
    store.tasks.subscribe(fn);
    store.tasks.updateTaskStatus('p1', 'a', 'RUNNING', '');
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
