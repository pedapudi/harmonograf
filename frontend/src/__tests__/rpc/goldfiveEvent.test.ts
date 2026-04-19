import { describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { SessionStore } from '../../gantt/index';
import { applyGoldfiveEvent } from '../../rpc/goldfiveEvent';
import {
  EventSchema,
  PlanSubmittedSchema,
  PlanRevisedSchema,
  TaskStartedSchema,
  TaskCompletedSchema,
  TaskFailedSchema,
  TaskBlockedSchema,
  TaskCancelledSchema,
  DriftDetectedSchema,
  RunStartedSchema,
} from '../../pb/goldfive/v1/events_pb';
import {
  PlanSchema,
  TaskSchema,
  TaskStatus,
  DriftKind,
  DriftSeverity,
} from '../../pb/goldfive/v1/types_pb';

function makePbTask(id: string, status: TaskStatus = TaskStatus.PENDING) {
  return create(TaskSchema, {
    id,
    title: `task ${id}`,
    description: '',
    assigneeAgentId: 'agent-a',
    status,
    predictedStartMs: 0n,
    predictedDurationMs: 0n,
  });
}

function makePbPlan(id: string, taskIds: string[]) {
  return create(PlanSchema, {
    id,
    runId: 'run-1',
    summary: `plan ${id}`,
    tasks: taskIds.map((t) => makePbTask(t)),
    edges: [],
    revisionReason: '',
    revisionKind: DriftKind.UNSPECIFIED,
    revisionSeverity: DriftSeverity.UNSPECIFIED,
    revisionIndex: 0,
  });
}

describe('applyGoldfiveEvent', () => {
  it('planSubmitted upserts a plan with four tasks', () => {
    const store = new SessionStore();
    const event = create(EventSchema, {
      eventId: 'ev-1',
      runId: 'run-1',
      sequence: 0n,
      payload: {
        case: 'planSubmitted',
        value: create(PlanSubmittedSchema, {
          plan: makePbPlan('p1', ['t1', 't2', 't3', 't4']),
        }),
      },
    });

    applyGoldfiveEvent(event, store, 0);

    const plans = store.tasks.listPlans();
    expect(plans).toHaveLength(1);
    expect(plans[0].id).toBe('p1');
    expect(plans[0].tasks.map((t) => t.id)).toEqual(['t1', 't2', 't3', 't4']);
    expect(plans[0].tasks.every((t) => t.status === 'PENDING')).toBe(true);
  });

  it('task_started / task_completed / task_failed / task_blocked / task_cancelled mutate status by task_id', () => {
    const store = new SessionStore();
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-0',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, {
            plan: makePbPlan('p1', ['t-run', 't-done', 't-fail', 't-block', 't-cancel']),
          }),
        },
      }),
      store,
      0,
    );

    // Each dispatch carries a different payload type; cast the payload
    // to bypass TS's oneof-narrowing. Test glue only.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const dispatch = (caseName: string, schema: any, taskId: string) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const payload = { case: caseName, value: create(schema, { taskId }) } as any;
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: `ev-${caseName}`,
          runId: 'run-1',
          sequence: 1n,
          payload,
        }),
        store,
        0,
      );
    };

    dispatch('taskStarted', TaskStartedSchema, 't-run');
    dispatch('taskCompleted', TaskCompletedSchema, 't-done');
    dispatch('taskFailed', TaskFailedSchema, 't-fail');
    dispatch('taskBlocked', TaskBlockedSchema, 't-block');
    dispatch('taskCancelled', TaskCancelledSchema, 't-cancel');

    const tasks = Object.fromEntries(
      store.tasks.getPlan('p1')!.tasks.map((t) => [t.id, t.status]),
    );
    expect(tasks).toEqual({
      't-run': 'RUNNING',
      't-done': 'COMPLETED',
      't-fail': 'FAILED',
      't-block': 'BLOCKED',
      't-cancel': 'CANCELLED',
    });
  });

  it('planRevised with non-zero revision_index updates the plan and maps drift kind/severity to lowercase', () => {
    const store = new SessionStore();
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-0',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, { plan: makePbPlan('p1', ['t1']) }),
        },
      }),
      store,
      0,
    );

    const revised = makePbPlan('p1', ['t1', 't2']);
    revised.revisionIndex = 1;
    revised.revisionReason = 'new work discovered';
    revised.revisionKind = DriftKind.NEW_WORK_DISCOVERED;
    revised.revisionSeverity = DriftSeverity.WARNING;

    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-1',
        runId: 'run-1',
        sequence: 1n,
        payload: {
          case: 'planRevised',
          value: create(PlanRevisedSchema, {
            plan: revised,
            driftKind: DriftKind.NEW_WORK_DISCOVERED,
            severity: DriftSeverity.WARNING,
            reason: 'new work discovered',
            revisionIndex: 1,
          }),
        },
      }),
      store,
      0,
    );

    const plan = store.tasks.getPlan('p1')!;
    expect(plan.tasks.map((t) => t.id)).toEqual(['t1', 't2']);
    expect(plan.revisionIndex).toBe(1);
    expect(plan.revisionReason).toBe('new work discovered');
    expect(plan.revisionKind).toBe('new_work_discovered');
    expect(plan.revisionSeverity).toBe('warning');
  });

  it('task event for unknown task_id is a no-op (plan not yet delivered)', () => {
    const store = new SessionStore();
    expect(() =>
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-orphan',
          runId: 'run-1',
          sequence: 0n,
          payload: {
            case: 'taskStarted',
            value: create(TaskStartedSchema, { taskId: 'unknown' }),
          },
        }),
        store,
        0,
      ),
    ).not.toThrow();
    expect(store.tasks.size).toBe(0);
  });

  it('drift_detected / run_started are accepted without mutating the task store', () => {
    const store = new SessionStore();
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-0',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, { plan: makePbPlan('p1', ['t1']) }),
        },
      }),
      store,
      0,
    );

    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-d',
        runId: 'run-1',
        sequence: 1n,
        payload: {
          case: 'driftDetected',
          value: create(DriftDetectedSchema, {
            kind: DriftKind.PLAN_DIVERGENCE,
            severity: DriftSeverity.WARNING,
            detail: 'off plan',
          }),
        },
      }),
      store,
      0,
    );
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-r',
        runId: 'run-1',
        sequence: 2n,
        payload: {
          case: 'runStarted',
          value: create(RunStartedSchema, {
            runId: 'run-1',
            goalSummary: 'do the thing',
          }),
        },
      }),
      store,
      0,
    );

    expect(store.tasks.getPlan('p1')!.tasks[0].status).toBe('PENDING');
  });
});
