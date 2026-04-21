import { beforeEach, describe, expect, it } from 'vitest';
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
  ApprovalRequestedSchema,
  ApprovalGrantedSchema,
  ApprovalRejectedSchema,
  AgentInvocationStartedSchema,
  AgentInvocationCompletedSchema,
  DelegationObservedSchema,
} from '../../pb/goldfive/v1/events_pb';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import {
  PlanSchema,
  TaskSchema,
  TaskStatus,
  DriftKind,
  DriftSeverity,
} from '../../pb/goldfive/v1/types_pb';
import { useApprovalsStore } from '../../state/approvalsStore';

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

  describe('approval events', () => {
    beforeEach(() => {
      useApprovalsStore.setState({ bySession: new Map() });
    });

    it('approvalRequested pushes a PendingApproval into the approvals store', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-ar',
          runId: 'run-1',
          sequence: 0n,
          payload: {
            case: 'approvalRequested',
            value: create(ApprovalRequestedSchema, {
              targetId: 'adk-call-1',
              kind: 'tool',
              prompt: 'Run write_file(path=/etc/passwd)?',
              taskId: 't-1',
              metadata: {
                tool_name: 'write_file',
                args_json: '{"path": "/etc/passwd"}',
              },
            }),
          },
        }),
        store,
        0,
        'sess-x',
      );

      const list = useApprovalsStore.getState().list('sess-x');
      expect(list).toHaveLength(1);
      expect(list[0].targetId).toBe('adk-call-1');
      expect(list[0].kind).toBe('tool');
      expect(list[0].prompt).toBe('Run write_file(path=/etc/passwd)?');
      expect(list[0].taskId).toBe('t-1');
      expect(list[0].metadata.tool_name).toBe('write_file');
      expect(list[0].metadata.args_json).toBe('{"path": "/etc/passwd"}');
    });

    it('approvalRequested without a sessionId is a no-op', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-ar',
          runId: 'run-1',
          sequence: 0n,
          payload: {
            case: 'approvalRequested',
            value: create(ApprovalRequestedSchema, {
              targetId: 't-1',
              kind: 'task',
              prompt: 'ok?',
              taskId: 't-1',
            }),
          },
        }),
        store,
        0,
      );
      expect(useApprovalsStore.getState().bySession.size).toBe(0);
    });

    it('approvalGranted dismisses the matching ApprovalRequested', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-ar',
          runId: 'run-1',
          sequence: 0n,
          payload: {
            case: 'approvalRequested',
            value: create(ApprovalRequestedSchema, {
              targetId: 't-1',
              kind: 'task',
              prompt: 'ok?',
              taskId: 't-1',
            }),
          },
        }),
        store,
        0,
        'sess-x',
      );
      expect(useApprovalsStore.getState().list('sess-x')).toHaveLength(1);

      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-ag',
          runId: 'run-1',
          sequence: 1n,
          payload: {
            case: 'approvalGranted',
            value: create(ApprovalGrantedSchema, {
              targetId: 't-1',
              detail: 'user approved',
            }),
          },
        }),
        store,
        0,
        'sess-x',
      );
      expect(useApprovalsStore.getState().list('sess-x')).toHaveLength(0);
    });

    it('approvalRejected dismisses the matching ApprovalRequested', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-ar',
          runId: 'run-1',
          sequence: 0n,
          payload: {
            case: 'approvalRequested',
            value: create(ApprovalRequestedSchema, {
              targetId: 't-1',
              kind: 'task',
              prompt: 'ok?',
              taskId: 't-1',
            }),
          },
        }),
        store,
        0,
        'sess-x',
      );

      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-aj',
          runId: 'run-1',
          sequence: 1n,
          payload: {
            case: 'approvalRejected',
            value: create(ApprovalRejectedSchema, {
              targetId: 't-1',
              detail: 'nope',
            }),
          },
        }),
        store,
        0,
        'sess-x',
      );
      expect(useApprovalsStore.getState().list('sess-x')).toHaveLength(0);
    });

    it('binds the approval to the assignee agent when the plan has seen the task', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-plan',
          runId: 'run-1',
          sequence: 0n,
          payload: {
            case: 'planSubmitted',
            value: create(PlanSubmittedSchema, {
              plan: makePbPlan('p1', ['t-1']),
            }),
          },
        }),
        store,
        0,
        'sess-x',
      );
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-ar',
          runId: 'run-1',
          sequence: 1n,
          payload: {
            case: 'approvalRequested',
            value: create(ApprovalRequestedSchema, {
              targetId: 't-1',
              kind: 'task',
              prompt: 'ok?',
              taskId: 't-1',
            }),
          },
        }),
        store,
        0,
        'sess-x',
      );
      const [entry] = useApprovalsStore.getState().list('sess-x');
      // makePbTask sets assigneeAgentId = 'agent-a'.
      expect(entry.agentId).toBe('agent-a');
    });
  });

  describe('registry-dispatch events (goldfive 2986775+)', () => {
    it('delegationObserved appends a DelegationRecord with wire fields mapped through', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-del',
          runId: 'run-1',
          sequence: 1n,
          // emittedAt = 5000ms wall-clock. With sessionStartMs=2000, the
          // recorded observedAtMs should land at 3000.
          emittedAt: create(TimestampSchema, {
            seconds: 5n,
            nanos: 0,
          }),
          payload: {
            case: 'delegationObserved',
            value: create(DelegationObservedSchema, {
              fromAgent: 'coordinator',
              toAgent: 'researcher',
              taskId: 't-42',
              invocationId: 'inv-7',
            }),
          },
        }),
        store,
        2000,
      );

      const list = store.delegations.list();
      expect(list).toHaveLength(1);
      expect(list[0]).toMatchObject({
        seq: 0,
        fromAgentId: 'coordinator',
        toAgentId: 'researcher',
        taskId: 't-42',
        invocationId: 'inv-7',
        observedAtMs: 3000,
      });
    });

    it('delegationObserved without emittedAt records observedAtMs=0', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-del2',
          runId: 'run-1',
          sequence: 0n,
          payload: {
            case: 'delegationObserved',
            value: create(DelegationObservedSchema, {
              fromAgent: 'a',
              toAgent: 'b',
              taskId: '',
              invocationId: 'inv',
            }),
          },
        }),
        store,
        0,
      );
      expect(store.delegations.list()[0].observedAtMs).toBe(0);
    });

    it('agentInvocationStarted is a no-op (telemetry plugin already emits INVOCATION spans)', () => {
      const store = new SessionStore();
      expect(() =>
        applyGoldfiveEvent(
          create(EventSchema, {
            eventId: 'ev-ais',
            runId: 'run-1',
            sequence: 0n,
            payload: {
              case: 'agentInvocationStarted',
              value: create(AgentInvocationStartedSchema, {
                agentName: 'coordinator',
                taskId: 't-1',
                invocationId: 'inv-1',
                parentInvocationId: '',
              }),
            },
          }),
          store,
          0,
        ),
      ).not.toThrow();
      // Should not have leaked into any store.
      expect(store.delegations.list()).toHaveLength(0);
      expect(store.spans.size).toBe(0);
      expect(store.agents.size).toBe(0);
      expect(store.tasks.size).toBe(0);
    });

    it('agentInvocationCompleted is a no-op', () => {
      const store = new SessionStore();
      expect(() =>
        applyGoldfiveEvent(
          create(EventSchema, {
            eventId: 'ev-aic',
            runId: 'run-1',
            sequence: 0n,
            payload: {
              case: 'agentInvocationCompleted',
              value: create(AgentInvocationCompletedSchema, {
                agentName: 'coordinator',
                taskId: 't-1',
                invocationId: 'inv-1',
                summary: 'ok',
              }),
            },
          }),
          store,
          0,
        ),
      ).not.toThrow();
      expect(store.delegations.list()).toHaveLength(0);
      expect(store.spans.size).toBe(0);
      expect(store.agents.size).toBe(0);
      expect(store.tasks.size).toBe(0);
    });
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
