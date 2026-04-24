// harmonograf#196 — intervention detail resolver.
//
// Verifies Trigger / Steering / Target composition given a drift + plan
// rev + SessionStore, and that forward-compat fields (trigger_input,
// refine input/output summaries, target agent) surface when present.

import { beforeEach, describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { SessionStore } from '../../gantt/index';
import { applyGoldfiveEvent } from '../../rpc/goldfiveEvent';
import {
  EventSchema,
  PlanSubmittedSchema,
  PlanRevisedSchema,
  DriftDetectedSchema,
} from '../../pb/goldfive/v1/events_pb';
import {
  PlanSchema,
  TaskSchema,
  DriftKind,
  DriftSeverity,
  TaskStatus,
} from '../../pb/goldfive/v1/types_pb';
import {
  resolveDriftDetail,
  resolvePlanRevisionDetail,
  resolveTaskCancelDetail,
} from '../../lib/interventionDetail';
import type { Task, TaskPlan } from '../../gantt/types';

function pbTask(id: string, agent = 'agent-a') {
  return create(TaskSchema, {
    id,
    title: `task ${id}`,
    description: '',
    assigneeAgentId: agent,
    status: TaskStatus.PENDING,
    predictedStartMs: 0n,
    predictedDurationMs: 0n,
  });
}

function pbPlan(id: string, taskIds: string[], revisionIndex = 0) {
  return create(PlanSchema, {
    id,
    runId: 'run-1',
    summary: `plan ${id}`,
    tasks: taskIds.map((t) => pbTask(t)),
    edges: [],
    revisionReason: '',
    revisionKind: DriftKind.UNSPECIFIED,
    revisionSeverity: DriftSeverity.UNSPECIFIED,
    revisionIndex,
  });
}

function ts(seconds: number) {
  return create(TimestampSchema, { seconds: BigInt(seconds), nanos: 0 });
}

describe('interventionDetail', () => {
  let store: SessionStore;
  beforeEach(() => {
    store = new SessionStore();
  });

  it('resolveDriftDetail surfaces drift detail + drift target', () => {
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(30),
        payload: {
          case: 'driftDetected',
          value: create(DriftDetectedSchema, {
            kind: DriftKind.LOOPING_REASONING,
            severity: DriftSeverity.WARNING,
            detail: 'repeated tool calls',
            currentTaskId: 't1',
            currentAgentId: 'agent-a',
            id: 'drift-1',
          }),
        },
      }),
      store,
      0,
    );

    const drifts = store.drifts.list();
    expect(drifts).toHaveLength(1);
    const detail = resolveDriftDetail(drifts[0], [], store);
    expect(detail.trigger).toBe('repeated tool calls');
    expect(detail.targetAgentId).toBe('agent-a');
    expect(detail.targetTaskId).toBe('t1');
    expect(detail.steering).toBe('');
  });

  it('resolveDriftDetail composes steering when a PlanRevised was triggered by the drift', () => {
    // Emit drift first (id=drift-X). Emit a plan rev whose triggerEventId
    // matches the drift id.
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-init',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, { plan: pbPlan('p1', ['t1']) }),
        },
      }),
      store,
      0,
    );
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-drift',
        runId: 'run-1',
        sequence: 1n,
        emittedAt: ts(30),
        payload: {
          case: 'driftDetected',
          value: create(DriftDetectedSchema, {
            kind: DriftKind.LOOPING_REASONING,
            severity: DriftSeverity.WARNING,
            detail: 'agent looping',
            currentTaskId: 't1',
            currentAgentId: 'agent-a',
            id: 'drift-42',
          }),
        },
      }),
      store,
      0,
    );
    const rev = pbPlan('p1', ['t1', 't2'], 1);
    rev.revisionReason = 'add verification step';
    rev.revisionKind = DriftKind.LOOPING_REASONING;
    rev.revisionTriggerEventId = 'drift-42';
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-rev',
        runId: 'run-1',
        sequence: 2n,
        emittedAt: ts(31),
        payload: {
          case: 'planRevised',
          value: create(PlanRevisedSchema, {
            plan: rev,
            driftKind: DriftKind.LOOPING_REASONING,
            reason: 'add verification step',
            revisionIndex: 1,
          }),
        },
      }),
      store,
      0,
    );

    const drifts = store.drifts.list();
    const plans: TaskPlan[] = [];
    for (const live of store.tasks.listPlans()) {
      for (const snap of store.tasks.allRevsForPlan(live.id)) plans.push(snap);
    }
    const detail = resolveDriftDetail(drifts[0], plans, store);
    expect(detail.trigger).toContain('agent looping');
    expect(detail.steering).toContain('add verification step');
    expect(detail.targetAgentId).toBe('agent-a');
  });

  it('resolveDriftDetail merges trigger_input (post-merge) into the Trigger section', () => {
    const d = create(DriftDetectedSchema, {
      kind: DriftKind.LOOPING_REASONING,
      severity: DriftSeverity.WARNING,
      detail: 'loop',
      currentTaskId: 't1',
      currentAgentId: 'agent-a',
      id: 'drift-5',
    });
    (d as unknown as Record<string, unknown>).triggerInput =
      'three search calls in a row';

    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(40),
        payload: { case: 'driftDetected', value: d },
      }),
      store,
      0,
    );
    const detail = resolveDriftDetail(store.drifts.list()[0], [], store);
    expect(detail.trigger).toContain('loop');
    expect(detail.trigger).toContain('three search calls');
  });

  it('resolveDriftDetail prefers refine.target_agent_id over drift.current_agent_id when present', () => {
    // Drift blames agent-a; the subsequent refine rebinds steering to
    // agent-b. The resolver should report agent-b as the steering target.
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-init',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, { plan: pbPlan('p1', ['t1']) }),
        },
      }),
      store,
      0,
    );
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-drift',
        runId: 'run-1',
        sequence: 1n,
        emittedAt: ts(30),
        payload: {
          case: 'driftDetected',
          value: create(DriftDetectedSchema, {
            kind: DriftKind.CONFABULATION_RISK,
            severity: DriftSeverity.WARNING,
            detail: 'loose claims',
            currentTaskId: 't1',
            currentAgentId: 'agent-a',
            id: 'drift-9',
          }),
        },
      }),
      store,
      0,
    );
    const rev = pbPlan('p1', ['t1'], 1);
    rev.revisionReason = 'tighten claims';
    rev.revisionTriggerEventId = 'drift-9';
    const prMsg = create(PlanRevisedSchema, {
      plan: rev,
      reason: 'tighten claims',
      revisionIndex: 1,
    });
    (prMsg as unknown as Record<string, unknown>).targetAgentId = 'agent-b';
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-rev',
        runId: 'run-1',
        sequence: 2n,
        emittedAt: ts(31),
        payload: { case: 'planRevised', value: prMsg },
      }),
      store,
      0,
    );
    const plans: TaskPlan[] = [];
    for (const live of store.tasks.listPlans()) {
      for (const snap of store.tasks.allRevsForPlan(live.id)) plans.push(snap);
    }
    const detail = resolveDriftDetail(store.drifts.list()[0], plans, store);
    expect(detail.targetAgentId).toBe('agent-b');
  });

  it('resolvePlanRevisionDetail returns steering + target for a goldfive-authored rev', () => {
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-init',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'planSubmitted',
          value: create(PlanSubmittedSchema, { plan: pbPlan('p1', ['t1']) }),
        },
      }),
      store,
      0,
    );
    const rev = pbPlan('p1', ['t1'], 1);
    rev.revisionReason = 'cascade cancel upstream';
    rev.revisionKind = DriftKind.UNSPECIFIED;
    const prMsg = create(PlanRevisedSchema, {
      plan: rev,
      reason: 'cascade cancel upstream',
      revisionIndex: 1,
    });
    (prMsg as unknown as Record<string, unknown>).targetAgentId = 'agent-c';
    (prMsg as unknown as Record<string, unknown>).refineInputSummary = 'upstream failed';
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-rev',
        runId: 'run-1',
        sequence: 1n,
        emittedAt: ts(100),
        payload: { case: 'planRevised', value: prMsg },
      }),
      store,
      0,
    );
    const plans: TaskPlan[] = [];
    for (const live of store.tasks.listPlans()) {
      for (const snap of store.tasks.allRevsForPlan(live.id)) plans.push(snap);
    }
    const revPlan = plans.find((p) => (p.revisionIndex ?? 0) === 1)!;
    const detail = resolvePlanRevisionDetail(revPlan, store);
    expect(detail.trigger).toBe('');
    expect(detail.steering).toContain('cascade cancel upstream');
    expect(detail.steering).toContain('upstream failed');
    expect(detail.targetAgentId).toBe('agent-c');
  });

  it('resolveTaskCancelDetail surfaces cancel_reason as steering', () => {
    const task: Task = {
      id: 't1',
      title: 'task 1',
      description: '',
      assigneeAgentId: 'agent-a',
      status: 'CANCELLED',
      predictedStartMs: 0,
      predictedDurationMs: 0,
      boundSpanId: '',
      cancelReason: 'upstream_failed:t0',
      supersedes: '',
    };
    const detail = resolveTaskCancelDetail(task);
    expect(detail.steering).toBe('upstream_failed:t0');
    expect(detail.targetAgentId).toBe('agent-a');
    expect(detail.targetTaskId).toBe('t1');
    expect(detail.trigger).toBe('');
  });

  it('resolveTaskCancelDetail returns empty on non-terminal tasks', () => {
    const task: Task = {
      id: 't1',
      title: 'task 1',
      description: '',
      assigneeAgentId: 'agent-a',
      status: 'RUNNING',
      predictedStartMs: 0,
      predictedDurationMs: 0,
      boundSpanId: '',
      supersedes: '',
    };
    const detail = resolveTaskCancelDetail(task);
    expect(detail.steering).toBe('');
    expect(detail.targetAgentId).toBe('');
  });
});
