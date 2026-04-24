import { beforeEach, describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { SessionStore } from '../../gantt/index';
import {
  PlanHistoryRegistry,
  type PlanRevisionRecord,
} from '../../state/planHistoryStore';
import { applyGoldfiveEvent } from '../../rpc/goldfiveEvent';
import { loadPlanHistory, revisionFromWire } from '../../state/planHistoryLoader';
import type { Task, TaskPlan } from '../../gantt/types';
import {
  EventSchema,
  PlanSubmittedSchema,
  PlanRevisedSchema,
} from '../../pb/goldfive/v1/events_pb';
import {
  PlanSchema,
  TaskSchema,
  TaskEdgeSchema,
  TaskStatus,
  DriftKind,
  DriftSeverity,
} from '../../pb/goldfive/v1/types_pb';

function mkTask(
  id: string,
  overrides: Partial<Task> = {},
): Task {
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
    reason: '',
    kind: '',
    triggerEventId: '',
    emittedAtMs: revision * 1000,
    ...overrides,
  };
}

// ── Registry unit tests ───────────────────────────────────────────────

describe('PlanHistoryRegistry', () => {
  let reg: PlanHistoryRegistry;
  beforeEach(() => {
    reg = new PlanHistoryRegistry();
  });

  it('append is idempotent on (plan_id, revision)', () => {
    reg.append(mkRecord('p1', 0, [mkTask('t1'), mkTask('t2')]));
    reg.append(mkRecord('p1', 0, [mkTask('t1'), mkTask('t2')]));
    reg.append(mkRecord('p1', 0, [mkTask('t1')])); // different payload, same rev — still deduped
    expect(reg.historyFor('p1')).toHaveLength(1);
    // Dedup keeps the FIRST-seen payload (explicit contract — subsequent
    // duplicates are dropped, not overwritten).
    expect(reg.historyFor('p1')[0].plan.tasks.map((t) => t.id)).toEqual([
      't1',
      't2',
    ]);
  });

  it('historyFor returns records sorted by revision number', () => {
    // Intentionally append out of order.
    reg.append(mkRecord('p1', 2, [mkTask('t1'), mkTask('t3')]));
    reg.append(mkRecord('p1', 0, [mkTask('t1')]));
    reg.append(mkRecord('p1', 1, [mkTask('t1'), mkTask('t2')]));
    const revs = reg.historyFor('p1').map((r) => r.revision);
    expect(revs).toEqual([0, 1, 2]);
  });

  it('historyFor returns an empty array for unknown plan ids', () => {
    expect(reg.historyFor('nope')).toEqual([]);
  });

  it('planAtRevision(0) returns the initial plan', () => {
    reg.append(mkRecord('p1', 0, [mkTask('t1')]));
    reg.append(mkRecord('p1', 1, [mkTask('t1'), mkTask('t2')]));
    const initial = reg.planAtRevision('p1', 0);
    expect(initial).not.toBeNull();
    expect(initial!.tasks.map((t) => t.id)).toEqual(['t1']);
    expect(reg.planAtRevision('p1', 5)).toBeNull();
    expect(reg.planAtRevision('no-plan', 0)).toBeNull();
  });

  it('append deep-clones the plan so later mutations do not leak', () => {
    const task = mkTask('t1', { status: 'PENDING' });
    const plan = mkPlan('p1', [task], 0);
    reg.append({
      revision: 0,
      plan,
      reason: '',
      kind: '',
      triggerEventId: '',
      emittedAtMs: 0,
    });
    // Mutate the source after append.
    task.status = 'RUNNING';
    plan.tasks[0].status = 'COMPLETED';
    const snapshot = reg.planAtRevision('p1', 0);
    expect(snapshot!.tasks[0].status).toBe('PENDING');
  });

  it('cumulativePlan unions tasks across revisions and flags superseded', () => {
    // rev 0: t1, t2
    reg.append(mkRecord('p1', 0, [mkTask('t1'), mkTask('t2')]));
    // rev 1: t1 edits (title change), t3 added, t2 dropped
    reg.append(
      mkRecord(
        'p1',
        1,
        [
          mkTask('t1', { title: 'Task t1 (edited)' }),
          mkTask('t3'),
        ],
        { reason: 'user wanted faster', kind: 'user_steer' },
      ),
    );
    // rev 2: t1 untouched, t3 edited
    reg.append(
      mkRecord('p1', 2, [
        mkTask('t1', { title: 'Task t1 (edited)' }),
        mkTask('t3', { description: 'now with detail' }),
      ]),
    );

    const cum = reg.cumulativePlan('p1');
    expect(cum).not.toBeNull();
    // All three tasks present (superseded t2 retained).
    const ids = cum!.tasks.map((t) => t.id);
    expect(ids.sort()).toEqual(['t1', 't2', 't3']);

    const t1Meta = cum!.taskRevisionMeta.get('t1')!;
    expect(t1Meta.introducedInRevision).toBe(0);
    expect(t1Meta.lastModifiedInRevision).toBe(1);
    expect(t1Meta.isSuperseded).toBe(false);

    const t2Meta = cum!.taskRevisionMeta.get('t2')!;
    expect(t2Meta.introducedInRevision).toBe(0);
    expect(t2Meta.isSuperseded).toBe(true);

    const t3Meta = cum!.taskRevisionMeta.get('t3')!;
    expect(t3Meta.introducedInRevision).toBe(1);
    expect(t3Meta.lastModifiedInRevision).toBe(2);
    expect(t3Meta.isSuperseded).toBe(false);
  });

  it('cumulativePlan returns null for an unknown plan id', () => {
    expect(reg.cumulativePlan('nope')).toBeNull();
  });

  it('supersedesMap extracts links annotated with kind/reason/trigger', () => {
    reg.append(mkRecord('p1', 0, [mkTask('t1'), mkTask('t2')]));
    // rev 1: t2 → t2b because of an off-topic drift
    reg.append(
      mkRecord('p1', 1, [mkTask('t1'), mkTask('t2b')], {
        reason: 'off topic — reorient on goal',
        kind: 'off_topic',
        triggerEventId: 'drift-abc',
      }),
    );
    // rev 2: t1 retired outright (no replacement) via user steer
    reg.append(
      mkRecord('p1', 2, [mkTask('t2b')], {
        reason: 'user cancelled t1',
        kind: 'user_steer',
        triggerEventId: 'ann-xyz',
      }),
    );

    const map = reg.supersedesMap('p1');
    expect(map.size).toBe(2);
    const t2Link = map.get('t2')!;
    expect(t2Link).toEqual({
      oldTaskId: 't2',
      newTaskId: 't2b',
      revision: 1,
      kind: 'off_topic',
      reason: 'off topic — reorient on goal',
      triggerEventId: 'drift-abc',
    });
    const t1Link = map.get('t1')!;
    expect(t1Link.oldTaskId).toBe('t1');
    // No truly-new task in rev 2, so the link is a dangling retire.
    expect(t1Link.newTaskId).toBe('');
    expect(t1Link.revision).toBe(2);
    expect(t1Link.kind).toBe('user_steer');
    expect(t1Link.triggerEventId).toBe('ann-xyz');
  });

  it('supersedesMap is empty for a plan with only its initial rev', () => {
    reg.append(mkRecord('p1', 0, [mkTask('t1'), mkTask('t2')]));
    expect(reg.supersedesMap('p1').size).toBe(0);
  });

  it('subscribe + clear emits once and wipes', () => {
    reg.append(mkRecord('p1', 0, [mkTask('t1')]));
    let hits = 0;
    const unsub = reg.subscribe(() => hits++);
    reg.clear();
    expect(hits).toBe(1);
    expect(reg.historyFor('p1')).toEqual([]);
    // A second clear on an already-empty registry is a no-op.
    reg.clear();
    expect(hits).toBe(1);
    unsub();
  });
});

// ── Integration with the goldfive event stream ────────────────────────

function pbTask(id: string, status: TaskStatus = TaskStatus.PENDING) {
  return create(TaskSchema, {
    id,
    title: `Task ${id}`,
    description: '',
    assigneeAgentId: 'agent-a',
    status,
    predictedStartMs: 0n,
    predictedDurationMs: 0n,
  });
}

function pbPlan(
  id: string,
  taskIds: string[],
  opts: {
    revisionIndex?: number;
    revisionReason?: string;
    revisionKind?: DriftKind;
    triggerEventId?: string;
  } = {},
) {
  return create(PlanSchema, {
    id,
    runId: 'run-1',
    summary: '',
    tasks: taskIds.map((t) => pbTask(t)),
    edges:
      taskIds.length >= 2
        ? [
            create(TaskEdgeSchema, {
              fromTaskId: taskIds[0],
              toTaskId: taskIds[1],
            }),
          ]
        : [],
    revisionReason: opts.revisionReason ?? '',
    revisionKind: opts.revisionKind ?? DriftKind.UNSPECIFIED,
    revisionSeverity: DriftSeverity.UNSPECIFIED,
    revisionIndex: opts.revisionIndex ?? 0,
    revisionTriggerEventId: opts.triggerEventId ?? '',
  });
}

describe('planHistory ingestion via applyGoldfiveEvent', () => {
  it('accumulates plan_submitted + plan_revised onto store.planHistory', () => {
    const store = new SessionStore();
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-0',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, {
            plan: pbPlan('p1', ['t1', 't2'], { revisionIndex: 0 }),
          }),
        },
      }),
      store,
      0,
    );
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-1',
        runId: 'run-1',
        sequence: 1n,
        payload: {
          case: 'planRevised',
          value: create(PlanRevisedSchema, {
            plan: pbPlan('p1', ['t1', 't3'], {
              revisionIndex: 1,
              revisionReason: 'off topic',
              revisionKind: DriftKind.OFF_TOPIC,
              triggerEventId: 'drift-1',
            }),
            reason: 'off topic',
            driftKind: DriftKind.OFF_TOPIC,
            revisionIndex: 1,
          }),
        },
      }),
      store,
      0,
    );
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-2',
        runId: 'run-1',
        sequence: 2n,
        payload: {
          case: 'planRevised',
          value: create(PlanRevisedSchema, {
            plan: pbPlan('p1', ['t1', 't3', 't4'], {
              revisionIndex: 2,
              revisionReason: 'user wants scope expanded',
              revisionKind: DriftKind.USER_STEER,
              triggerEventId: 'ann-2',
            }),
            reason: 'user wants scope expanded',
            driftKind: DriftKind.USER_STEER,
            revisionIndex: 2,
          }),
        },
      }),
      store,
      0,
    );

    const history = store.planHistory.historyFor('p1');
    expect(history.map((r) => r.revision)).toEqual([0, 1, 2]);
    expect(history[1].kind).toBe('off_topic');
    expect(history[1].triggerEventId).toBe('drift-1');
    expect(history[2].kind).toBe('user_steer');

    const cum = store.planHistory.cumulativePlan('p1')!;
    const cumIds = cum.tasks.map((t) => t.id).sort();
    expect(cumIds).toEqual(['t1', 't2', 't3', 't4']);
    expect(cum.taskRevisionMeta.get('t2')!.isSuperseded).toBe(true);
    expect(cum.taskRevisionMeta.get('t4')!.introducedInRevision).toBe(2);

    const sup = store.planHistory.supersedesMap('p1');
    expect(sup.get('t2')!.kind).toBe('off_topic');
    expect(sup.get('t2')!.newTaskId).toBe('t3');
  });

  it('replay of the same event is idempotent (no duplicate revision)', () => {
    const store = new SessionStore();
    const mkEvent = () =>
      create(EventSchema, {
        eventId: 'ev-0',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, {
            plan: pbPlan('p1', ['t1']),
          }),
        },
      });
    applyGoldfiveEvent(mkEvent(), store, 0);
    applyGoldfiveEvent(mkEvent(), store, 0);
    applyGoldfiveEvent(mkEvent(), store, 0);
    expect(store.planHistory.historyFor('p1')).toHaveLength(1);
  });
});

// ── RPC loader (graceful degradation) ─────────────────────────────────

describe('loadPlanHistory', () => {
  it('degrades gracefully when the client lacks getSessionPlanHistory', async () => {
    const store = new SessionStore();
    // Stub client with NO getSessionPlanHistory method — this mirrors
    // the current generated client (RPC hasn't landed yet).
    const stub = {};
    const result = await loadPlanHistory('sess-1', store, 0, stub);
    expect(result.skipped).toBe(true);
    expect(result.fetched).toBe(false);
    expect(result.appended).toBe(0);
    expect(result.error).toBeNull();
    expect(store.planHistory.historyFor('p1')).toEqual([]);
  });

  it('seeds the registry when the RPC returns revisions', async () => {
    const store = new SessionStore();
    const stub = {
      getSessionPlanHistory: async (req: { sessionId: string }) => {
        expect(req.sessionId).toBe('sess-1');
        return {
          revisions: [
            {
              plan: pbPlan('p1', ['t1', 't2'], { revisionIndex: 0 }),
              revisionNumber: 0,
              revisionReason: '',
              revisionKind: 'UNSPECIFIED',
              revisionTriggerEventId: '',
              emittedAt: { seconds: 1000n, nanos: 0 },
            },
            {
              plan: pbPlan('p1', ['t1', 't3'], {
                revisionIndex: 1,
                revisionReason: 'off topic',
                revisionKind: DriftKind.OFF_TOPIC,
                triggerEventId: 'drift-1',
              }),
              revisionNumber: 1,
              revisionReason: 'off topic',
              revisionKind: 'OFF_TOPIC',
              revisionTriggerEventId: 'drift-1',
              emittedAt: { seconds: 1010n, nanos: 0 },
            },
          ],
        };
      },
    };
    const result = await loadPlanHistory('sess-1', store, 0, stub);
    expect(result.skipped).toBe(false);
    expect(result.fetched).toBe(true);
    expect(result.appended).toBe(2);
    const revs = store.planHistory.historyFor('p1');
    expect(revs).toHaveLength(2);
    expect(revs[1].kind).toBe('off_topic');
    expect(revs[1].triggerEventId).toBe('drift-1');
  });

  it('records RPC error and leaves the registry untouched', async () => {
    const store = new SessionStore();
    const stub = {
      getSessionPlanHistory: async () => {
        throw new Error('unavailable');
      },
    };
    const result = await loadPlanHistory('sess-1', store, 0, stub);
    expect(result.skipped).toBe(false);
    expect(result.fetched).toBe(false);
    expect(result.error?.message).toBe('unavailable');
    expect(store.planHistory.historyFor('p1')).toEqual([]);
  });

  it('RPC-then-stream dedups on (plan_id, revision_number)', async () => {
    const store = new SessionStore();
    const stub = {
      getSessionPlanHistory: async () => ({
        revisions: [
          {
            plan: pbPlan('p1', ['t1']),
            revisionNumber: 0,
            revisionReason: '',
            revisionKind: '',
            revisionTriggerEventId: '',
            emittedAt: { seconds: 0n, nanos: 0 },
          },
        ],
      }),
    };
    await loadPlanHistory('sess-1', store, 0, stub);
    // Now simulate the live stream replaying the same plan_submitted.
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-0',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, {
            plan: pbPlan('p1', ['t1']),
          }),
        },
      }),
      store,
      0,
    );
    expect(store.planHistory.historyFor('p1')).toHaveLength(1);
  });

  it('revisionFromWire returns null when plan payload is missing', () => {
    expect(
      revisionFromWire({ revisionNumber: 0 }, 0),
    ).toBeNull();
  });
});
