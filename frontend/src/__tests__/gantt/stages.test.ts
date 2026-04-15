import { describe, expect, it, vi } from 'vitest';
import { computeStages } from '../../gantt/stages';
import type { Task, TaskEdge, TaskPlan } from '../../gantt/types';

function mkTask(id: string): Task {
  return {
    id,
    title: id,
    description: '',
    assigneeAgentId: '',
    status: 'PENDING',
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
  };
}

function mkPlan(tasks: string[], edges: Array<[string, string]>): TaskPlan {
  return {
    id: 'plan',
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks: tasks.map(mkTask),
    edges: edges.map<TaskEdge>(([f, t]) => ({ fromTaskId: f, toTaskId: t })),
    revisionReason: '',
  };
}

function stageIds(stages: Task[][]): string[][] {
  return stages.map((s) => s.map((t) => t.id).sort());
}

describe('computeStages', () => {
  it('returns [] for an empty plan', () => {
    expect(computeStages(mkPlan([], []))).toEqual([]);
  });

  it('returns one single-task stage for a single-node plan', () => {
    const s = computeStages(mkPlan(['t1'], []));
    expect(stageIds(s)).toEqual([['t1']]);
  });

  it('linear DAG places tasks in strict sequence', () => {
    const s = computeStages(
      mkPlan(['t1', 't2', 't3'], [['t1', 't2'], ['t2', 't3']]),
    );
    expect(stageIds(s)).toEqual([['t1'], ['t2'], ['t3']]);
  });

  it('diamond puts parallel mid-layer together', () => {
    const s = computeStages(
      mkPlan(
        ['t1', 't2', 't3', 't4'],
        [
          ['t1', 't2'],
          ['t1', 't3'],
          ['t2', 't4'],
          ['t3', 't4'],
        ],
      ),
    );
    expect(stageIds(s)).toEqual([['t1'], ['t2', 't3'], ['t4']]);
  });

  it('disconnected chains layer independently by depth', () => {
    const s = computeStages(
      mkPlan(
        ['a1', 'a2', 'b1', 'b2', 'b3'],
        [
          ['a1', 'a2'],
          ['b1', 'b2'],
          ['b2', 'b3'],
        ],
      ),
    );
    // Depths: a1=0, a2=1, b1=0, b2=1, b3=2
    expect(stageIds(s)).toEqual([['a1', 'b1'], ['a2', 'b2'], ['b3']]);
  });

  it('detects a cycle, collapses stranded tasks into stage 0, and warns', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const s = computeStages(
      mkPlan(['t1', 't2', 't3'], [['t1', 't2'], ['t2', 't3'], ['t3', 't1']]),
    );
    expect(warn).toHaveBeenCalledTimes(1);
    // All three tasks are stranded and land in stage 0.
    expect(stageIds(s)).toEqual([['t1', 't2', 't3']]);
    warn.mockRestore();
  });

  it('ignores edges referencing unknown task ids', () => {
    const s = computeStages(
      mkPlan(['t1', 't2'], [['t1', 't2'], ['t1', 'ghost'], ['ghost', 't2']]),
    );
    expect(stageIds(s)).toEqual([['t1'], ['t2']]);
  });
});
