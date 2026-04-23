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
  TaskEdgeSchema,
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

  // harmonograf#107 — edges must survive the proto→UI conversion. Without
  // this guard a regression in convertGoldfivePlan (e.g. a proto field
  // rename from `edges` to something else) silently empties the dependency
  // DAG so Gantt / TaskStagesGraph render with zero arrows.
  it('planSubmitted carries edges intact through convertGoldfivePlan', () => {
    const store = new SessionStore();
    const plan = create(PlanSchema, {
      id: 'p-edges',
      runId: 'run-1',
      summary: '7-stage',
      tasks: ['t1', 't2', 't3', 't4', 't5', 't6', 't7'].map((id) =>
        makePbTask(id),
      ),
      edges: [
        create(TaskEdgeSchema, { fromTaskId: 't1', toTaskId: 't2' }),
        create(TaskEdgeSchema, { fromTaskId: 't2', toTaskId: 't3' }),
        create(TaskEdgeSchema, { fromTaskId: 't2', toTaskId: 't4' }),
        create(TaskEdgeSchema, { fromTaskId: 't3', toTaskId: 't5' }),
        create(TaskEdgeSchema, { fromTaskId: 't4', toTaskId: 't5' }),
        create(TaskEdgeSchema, { fromTaskId: 't5', toTaskId: 't6' }),
        create(TaskEdgeSchema, { fromTaskId: 't6', toTaskId: 't7' }),
      ],
      revisionReason: '',
      revisionKind: DriftKind.UNSPECIFIED,
      revisionSeverity: DriftSeverity.UNSPECIFIED,
      revisionIndex: 0,
    });
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-edges',
        runId: 'run-1',
        sequence: 0n,
        payload: { case: 'planSubmitted', value: create(PlanSubmittedSchema, { plan }) },
      }),
      store,
      0,
    );
    const stored = store.tasks.getPlan('p-edges');
    expect(stored).toBeDefined();
    expect(stored!.edges).toHaveLength(7);
    expect(stored!.edges[0]).toEqual({ fromTaskId: 't1', toTaskId: 't2' });
    expect(stored!.edges[6]).toEqual({ fromTaskId: 't6', toTaskId: 't7' });
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

  // harmonograf#110 / goldfive#205: structured cancel reason on
  // TaskCancelled / TaskFailed envelopes rides through goldfiveEvent
  // onto ``Task.cancelReason`` so the three UI surfaces (TaskStagesGraph
  // tooltip, Drawer Overview section, TrajectoryView task-delta list)
  // all render it.
  it('task_cancelled / task_failed thread reason onto Task.cancelReason', () => {
    const store = new SessionStore();
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-0',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, {
            plan: makePbPlan('p1', ['t-cancel', 't-fail']),
          }),
        },
      }),
      store,
      0,
    );

    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-cancel',
        runId: 'run-1',
        sequence: 1n,
        payload: {
          case: 'taskCancelled',
          value: create(TaskCancelledSchema, {
            taskId: 't-cancel',
            reason: 'upstream_failed:parent',
          }),
        },
      }),
      store,
      0,
    );

    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-fail',
        runId: 'run-1',
        sequence: 2n,
        payload: {
          case: 'taskFailed',
          value: create(TaskFailedSchema, {
            taskId: 't-fail',
            reason: 'refine_validation_failed',
          }),
        },
      }),
      store,
      0,
    );

    const byId = Object.fromEntries(
      store.tasks.getPlan('p1')!.tasks.map((t) => [t.id, t]),
    );
    expect(byId['t-cancel'].status).toBe('CANCELLED');
    expect(byId['t-cancel'].cancelReason).toBe('upstream_failed:parent');
    expect(byId['t-fail'].status).toBe('FAILED');
    expect(byId['t-fail'].cancelReason).toBe('refine_validation_failed');
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

    // harmonograf#127: live-path race — a delegation_observed dispatched
    // BEFORE the 'session' SessionUpdate has seeded wallClockStartMs on
    // the store. Ingest stamps observedAtAbsoluteMs from emitted_at but
    // observedAtMs lands wall-clock-scale; when the session frame
    // finally lands and rebaseRelativeTimestamps fires, observedAtMs
    // must snap to the correct session-relative value so the Gantt /
    // Graph delegation arrow lines up.
    it('delegationObserved during the live-path race recovers after rebase', () => {
      const store = new SessionStore();
      // Pretend the session's wall-clock start is 2000ms epoch. The
      // delegation fires at 5000ms epoch — expected relative = 3000.
      // At ingest, though, wallClockStartMs hasn't been set, so the
      // caller passes sessionStartMs=0 (mirroring hooks.ts when
      // `origin` is still null).
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-del-race',
          runId: 'run-1',
          sequence: 0n,
          emittedAt: create(TimestampSchema, { seconds: 5n, nanos: 0 }),
          payload: {
            case: 'delegationObserved',
            value: create(DelegationObservedSchema, {
              fromAgent: 'coordinator',
              toAgent: 'researcher',
              taskId: 't-1',
              invocationId: 'inv-race',
            }),
          },
        }),
        store,
        0, // sessionStartMs not yet known on the frontend
      );

      const preRebase = store.delegations.list();
      expect(preRebase).toHaveLength(1);
      // Pre-rebase the relative value is wall-clock-scale (5000 - 0),
      // which would render miles off-axis on the Gantt timeline.
      expect(preRebase[0].observedAtMs).toBe(5000);
      // But the authoritative absolute ms is preserved.
      expect(preRebase[0].observedAtAbsoluteMs).toBe(5000);

      // Session frame finally lands — hooks.ts calls this to re-anchor
      // any pre-session events on the live path.
      store.rebaseRelativeTimestamps(2000);

      const postRebase = store.delegations.list();
      expect(postRebase).toHaveLength(1);
      expect(postRebase[0].observedAtMs).toBe(3000);
      // Absolute is invariant across rebases.
      expect(postRebase[0].observedAtAbsoluteMs).toBe(5000);
    });

    // Regression for the refresh path: when the session frame DOES
    // land first (burst replay order), ingest computes the right
    // relative value directly, and a subsequent (idempotent) rebase
    // with the same startMs is a no-op.
    it('delegationObserved on the refresh path yields the same observedAtMs as the rebased live path', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-del-refresh',
          runId: 'run-1',
          sequence: 0n,
          emittedAt: create(TimestampSchema, { seconds: 5n, nanos: 0 }),
          payload: {
            case: 'delegationObserved',
            value: create(DelegationObservedSchema, {
              fromAgent: 'coordinator',
              toAgent: 'researcher',
              taskId: 't-1',
              invocationId: 'inv-refresh',
            }),
          },
        }),
        store,
        2000, // sessionStartMs already known (burst delivered session first)
      );
      expect(store.delegations.list()[0].observedAtMs).toBe(3000);

      // Idempotent: rebasing with the same start must not shift anything.
      store.rebaseRelativeTimestamps(2000);
      expect(store.delegations.list()[0].observedAtMs).toBe(3000);
    });

    it('driftDetected during the live-path race recovers after rebase', () => {
      const store = new SessionStore();
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev-drift-race',
          runId: 'run-1',
          sequence: 0n,
          emittedAt: create(TimestampSchema, { seconds: 7n, nanos: 0 }),
          payload: {
            case: 'driftDetected',
            value: create(DriftDetectedSchema, {
              kind: DriftKind.BLOCKED,
              severity: DriftSeverity.WARNING,
              detail: 'race',
              currentTaskId: 't-1',
              currentAgentId: 'agent-a',
              id: 'drift-race-1',
            }),
          },
        }),
        store,
        0,
      );

      const pre = store.drifts.list();
      expect(pre).toHaveLength(1);
      expect(pre[0].recordedAtMs).toBe(7000);
      expect(pre[0].recordedAtAbsoluteMs).toBe(7000);

      store.rebaseRelativeTimestamps(3000);
      const post = store.drifts.list();
      expect(post[0].recordedAtMs).toBe(4000);
      expect(post[0].recordedAtAbsoluteMs).toBe(7000);
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
