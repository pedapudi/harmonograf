// Unit tests for PlanHistoryRegistry.supersedesMap's two-pass algorithm:
//
//   Pass 1 — authoritative: Task.supersedes (set by goldfive's refine LLM
//   per goldfive#237) produces a SupersessionLink keyed by the old id.
//
//   Pass 2 — positional heuristic fallback: only for dropped ids that
//   Pass 1 did NOT pair, pair by list order against unpaired truly-new
//   ids. This exists only for legacy plans / tasks where the LLM
//   omitted `supersedes`.
//
// These tests pin the invariants so the heuristic can't quietly
// mispair when the authoritative signal is present.

import { beforeEach, describe, expect, it } from 'vitest';
import {
  PlanHistoryRegistry,
  type PlanRevisionRecord,
} from '../../state/planHistoryStore';
import type { Task, TaskPlan } from '../../gantt/types';

function mkTask(id: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    title: `Task ${id}`,
    description: '',
    assigneeAgentId: 'agent-a',
    status: 'PENDING',
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    supersedes: '',
    ...overrides,
  };
}

function mkPlan(id: string, tasks: Task[], rev = 0): TaskPlan {
  return {
    id,
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks,
    edges: [],
    revisionReason: '',
    revisionKind: '',
    revisionSeverity: '',
    revisionIndex: rev,
    triggerEventId: '',
  };
}

function mkRecord(
  planId: string,
  revision: number,
  tasks: Task[],
  overrides: Partial<PlanRevisionRecord> = {},
): PlanRevisionRecord {
  return {
    revision,
    plan: mkPlan(planId, tasks, revision),
    reason: overrides.reason ?? `reason-${revision}`,
    kind: overrides.kind ?? (revision === 0 ? '' : 'off_topic'),
    triggerEventId:
      overrides.triggerEventId ?? (revision === 0 ? '' : `drift-${revision}`),
    emittedAtMs: overrides.emittedAtMs ?? revision * 1000,
  };
}

describe('PlanHistoryRegistry.supersedesMap (two-pass)', () => {
  let reg: PlanHistoryRegistry;

  beforeEach(() => {
    reg = new PlanHistoryRegistry();
  });

  it('returns an empty map when there is a single revision and no supersessions', () => {
    reg.append(mkRecord('p1', 0, [mkTask('a'), mkTask('b')]));
    const m = reg.supersedesMap('p1');
    expect(m.size).toBe(0);
  });

  it('authoritative pairing wins over the positional heuristic', () => {
    // rev 0: [a, b]
    // rev 1: [a, c] where c.supersedes = b → must pair b→c explicitly.
    reg.append(mkRecord('p1', 0, [mkTask('a'), mkTask('b')]));
    reg.append(
      mkRecord('p1', 1, [mkTask('a'), mkTask('c', { supersedes: 'b' })]),
    );
    const m = reg.supersedesMap('p1');
    expect(m.size).toBe(1);
    const link = m.get('b');
    expect(link).toBeDefined();
    expect(link?.oldTaskId).toBe('b');
    expect(link?.newTaskId).toBe('c');
    expect(link?.revision).toBe(1);
    expect(link?.kind).toBe('off_topic');
    expect(link?.triggerEventId).toBe('drift-1');
  });

  it('falls back to the positional heuristic when supersedes is empty', () => {
    // rev 0: [a, b]
    // rev 1: [a, c]  (c.supersedes = '' → heuristic pairs b→c)
    reg.append(mkRecord('p1', 0, [mkTask('a'), mkTask('b')]));
    reg.append(mkRecord('p1', 1, [mkTask('a'), mkTask('c')]));
    const m = reg.supersedesMap('p1');
    expect(m.size).toBe(1);
    expect(m.get('b')?.newTaskId).toBe('c');
    expect(m.get('b')?.revision).toBe(1);
  });

  it('mixes authoritative + heuristic when only some replacements are annotated', () => {
    // rev 0: [a, b]                 — two tasks.
    // rev 1: [c, d] — both dropped, two new.
    //   c.supersedes = 'a' (authoritative)
    //   d.supersedes = ''  (heuristic must pair d to b)
    reg.append(mkRecord('p1', 0, [mkTask('a'), mkTask('b')]));
    reg.append(
      mkRecord('p1', 1, [
        mkTask('c', { supersedes: 'a' }),
        mkTask('d'),
      ]),
    );
    const m = reg.supersedesMap('p1');
    expect(m.size).toBe(2);
    expect(m.get('a')?.newTaskId).toBe('c'); // authoritative
    expect(m.get('b')?.newTaskId).toBe('d'); // heuristic
    // Both should stamp rev 1's trigger metadata.
    expect(m.get('a')?.revision).toBe(1);
    expect(m.get('b')?.revision).toBe(1);
  });

  it('collapses a chain a → b → c across three revisions', () => {
    // rev 0: [a]
    // rev 1: [b] where b.supersedes = 'a'
    // rev 2: [c] where c.supersedes = 'b'
    reg.append(mkRecord('p1', 0, [mkTask('a')]));
    reg.append(
      mkRecord('p1', 1, [mkTask('b', { supersedes: 'a' })], {
        kind: 'off_topic',
        triggerEventId: 'drift-1',
      }),
    );
    reg.append(
      mkRecord('p1', 2, [mkTask('c', { supersedes: 'b' })], {
        kind: 'user_steer',
        triggerEventId: 'ann-2',
      }),
    );
    const m = reg.supersedesMap('p1');
    expect(m.size).toBe(2);

    const aLink = m.get('a');
    expect(aLink?.newTaskId).toBe('b');
    expect(aLink?.revision).toBe(1);
    expect(aLink?.kind).toBe('off_topic');
    expect(aLink?.triggerEventId).toBe('drift-1');

    const bLink = m.get('b');
    expect(bLink?.newTaskId).toBe('c');
    expect(bLink?.revision).toBe(2);
    expect(bLink?.kind).toBe('user_steer');
    expect(bLink?.triggerEventId).toBe('ann-2');
  });

  it('ignores a genuinely new task that does not replace anything', () => {
    // rev 0: [a, b]
    // rev 1: [a, c, d] where d.supersedes='b' and c is entirely new.
    // → only b appears as a key; c (genuinely new) is never a key.
    reg.append(mkRecord('p1', 0, [mkTask('a'), mkTask('b')]));
    reg.append(
      mkRecord('p1', 1, [
        mkTask('a'),
        mkTask('c'), // genuinely new, no supersedes
        mkTask('d', { supersedes: 'b' }),
      ]),
    );
    const m = reg.supersedesMap('p1');
    expect(m.size).toBe(1);
    expect(m.get('b')?.newTaskId).toBe('d');
    expect(m.has('c')).toBe(false);
    expect(m.has('a')).toBe(false);
  });

  it('Pass 1 emits at the revision where the replacement first appears', () => {
    // rev 0: [a]
    // rev 1: [b] (b.supersedes=a; should stamp rev 1)
    // rev 2: [b] (b carries through; must NOT re-stamp rev 2)
    reg.append(mkRecord('p1', 0, [mkTask('a')]));
    reg.append(
      mkRecord('p1', 1, [mkTask('b', { supersedes: 'a' })], {
        triggerEventId: 'drift-1',
      }),
    );
    reg.append(
      mkRecord('p1', 2, [mkTask('b', { supersedes: 'a' })], {
        triggerEventId: 'drift-2',
      }),
    );
    const link = reg.supersedesMap('p1').get('a');
    expect(link?.revision).toBe(1);
    expect(link?.triggerEventId).toBe('drift-1');
  });

  it('dropped ids beyond the unpaired-new count get empty newTaskId (dangling)', () => {
    // rev 0: [a, b]
    // rev 1: []  (both dropped, nothing new) → heuristic emits dangling
    //            edges for each dropped id with newTaskId=''.
    reg.append(mkRecord('p1', 0, [mkTask('a'), mkTask('b')]));
    reg.append(mkRecord('p1', 1, []));
    const m = reg.supersedesMap('p1');
    expect(m.size).toBe(2);
    expect(m.get('a')?.newTaskId).toBe('');
    expect(m.get('b')?.newTaskId).toBe('');
  });

  it('returns an empty map for an unknown planId', () => {
    reg.append(mkRecord('p1', 0, [mkTask('a')]));
    expect(reg.supersedesMap('p-unknown').size).toBe(0);
  });
});
