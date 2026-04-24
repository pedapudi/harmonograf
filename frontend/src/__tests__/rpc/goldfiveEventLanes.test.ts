// harmonograf#196: goldfive + user lane synthesis
//
// Verifies that ingest maps drift / plan-revised / run-started events onto
// synthesized spans on the __goldfive__ / __user__ actor rows so the Gantt
// and Trajectory views can render them as first-class activity.

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
  RunStartedSchema,
} from '../../pb/goldfive/v1/events_pb';
import {
  PlanSchema,
  TaskSchema,
  DriftKind,
  DriftSeverity,
  TaskStatus,
} from '../../pb/goldfive/v1/types_pb';
import { GOLDFIVE_ACTOR_ID, USER_ACTOR_ID } from '../../theme/agentColors';

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

describe('goldfive lane synthesis (harmonograf#196)', () => {
  let store: SessionStore;
  beforeEach(() => {
    store = new SessionStore();
  });

  it('planRevised synthesizes a refine span on the goldfive actor row', () => {
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-0',
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

    const revised = pbPlan('p1', ['t1', 't2'], 1);
    revised.revisionReason = 'loop detected';
    revised.revisionKind = DriftKind.LOOPING_REASONING;
    revised.revisionSeverity = DriftSeverity.WARNING;

    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-rev',
        runId: 'run-1',
        sequence: 1n,
        emittedAt: ts(60),
        payload: {
          case: 'planRevised',
          value: create(PlanRevisedSchema, {
            plan: revised,
            driftKind: DriftKind.LOOPING_REASONING,
            severity: DriftSeverity.WARNING,
            reason: 'loop detected',
            revisionIndex: 1,
          }),
        },
      }),
      store,
      0,
    );

    // Goldfive actor row exists.
    expect(store.agents.get(GOLDFIVE_ACTOR_ID)).toBeTruthy();

    // A refine span is stamped on the goldfive row.
    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans as never);
    const refine = spans.find((s) => s && s.name.startsWith('refine:'));
    expect(refine).toBeTruthy();
    if (!refine) return;
    expect(refine.agentId).toBe(GOLDFIVE_ACTOR_ID);
    expect(refine.kind).toBe('CUSTOM');
    expect(refine.attributes['refine.kind']).toEqual({
      kind: 'string',
      value: 'looping_reasoning',
    });
    expect(refine.attributes['refine.reason']).toEqual({
      kind: 'string',
      value: 'loop detected',
    });
    expect(refine.attributes['refine.index']).toEqual({
      kind: 'string',
      value: '1',
    });
  });

  it('planSubmitted (rev 0) does NOT synthesize a refine span', () => {
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-0',
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
    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans as never);
    expect(spans.filter((s) => s?.name.startsWith('refine:'))).toHaveLength(0);
  });

  it('runStarted synthesizes a USER_MESSAGE span on the user actor row carrying goal_summary', () => {
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-run',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(0),
        payload: {
          case: 'runStarted',
          value: create(RunStartedSchema, {
            runId: 'run-1',
            goalSummary: 'summarize the paper',
          }),
        },
      }),
      store,
      0,
    );

    expect(store.agents.get(USER_ACTOR_ID)).toBeTruthy();
    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(USER_ACTOR_ID, 0, 1_000_000, spans as never);
    const userSpan = spans.find((s) => s?.kind === 'USER_MESSAGE' && s.name === 'summarize the paper');
    expect(userSpan).toBeTruthy();
    expect(userSpan?.attributes['user.goal_summary']).toEqual({
      kind: 'string',
      value: 'summarize the paper',
    });
  });

  it('runStarted with an empty goal_summary skips span creation', () => {
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-run',
        runId: 'run-1',
        sequence: 0n,
        payload: {
          case: 'runStarted',
          value: create(RunStartedSchema, { runId: 'run-1', goalSummary: '' }),
        },
      }),
      store,
      0,
    );
    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(USER_ACTOR_ID, 0, 1_000_000, spans as never);
    expect(spans).toHaveLength(0);
  });

  it('driftDetected still synthesizes a goldfive-row span (pre-existing behaviour preserved)', () => {
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-drift',
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
    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans as never);
    expect(spans).toHaveLength(1);
    const drift = spans[0];
    expect(drift?.name).toBe('looping_reasoning');
    expect(drift?.attributes['drift.kind']).toEqual({
      kind: 'string',
      value: 'looping_reasoning',
    });
    expect(drift?.attributes['drift.target_agent_id']).toEqual({
      kind: 'string',
      value: 'agent-a',
    });
  });

  it('user_steer drift lands on the user row (not the goldfive row)', () => {
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-user-drift',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(10),
        payload: {
          case: 'driftDetected',
          value: create(DriftDetectedSchema, {
            kind: DriftKind.USER_STEER,
            severity: DriftSeverity.INFO,
            detail: 'please focus on section 2',
            currentTaskId: 't1',
            currentAgentId: 'agent-a',
            annotationId: 'ann-1',
            id: 'drift-2',
          }),
        },
      }),
      store,
      0,
    );
    const goldfive: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, goldfive as never);
    expect(goldfive).toHaveLength(0);
    const user: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(USER_ACTOR_ID, 0, 1_000_000, user as never);
    expect(user).toHaveLength(1);
    expect(user[0]?.kind).toBe('USER_MESSAGE');
  });

  it('forward-compat: PlanRevised with extra fields carries them onto the refine span', () => {
    const revised = pbPlan('p1', ['t1'], 2);
    // Simulate post-merge proto having the new fields by creating the
    // PlanRevised message and injecting the extra keys directly on the
    // object. The ingest path reads through `unknown` so this
    // mimics what the regenerated stubs will produce.
    const prMsg = create(PlanRevisedSchema, {
      plan: revised,
      driftKind: DriftKind.PLAN_DIVERGENCE,
      severity: DriftSeverity.WARNING,
      reason: 'divergence',
      revisionIndex: 2,
    });
    (prMsg as unknown as Record<string, unknown>).targetAgentId = 'agent-a';
    (prMsg as unknown as Record<string, unknown>).refineInputSummary =
      'agent-a stuck on step 3';
    (prMsg as unknown as Record<string, unknown>).refineOutputSummary =
      'add step 4: verify';

    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(40),
        payload: { case: 'planRevised', value: prMsg },
      }),
      store,
      0,
    );

    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans as never);
    const refine = spans.find((s) => s && s.name.startsWith('refine:'));
    expect(refine?.attributes['refine.target_agent_id']).toEqual({
      kind: 'string',
      value: 'agent-a',
    });
    expect(refine?.attributes['refine.input_summary']).toEqual({
      kind: 'string',
      value: 'agent-a stuck on step 3',
    });
    expect(refine?.attributes['refine.output_summary']).toEqual({
      kind: 'string',
      value: 'add step 4: verify',
    });
  });

  it('forward-compat: DriftDetected.trigger_input lands on the synthesized drift span', () => {
    const d = create(DriftDetectedSchema, {
      kind: DriftKind.LOOPING_REASONING,
      severity: DriftSeverity.WARNING,
      detail: 'repeated tool',
      currentTaskId: 't1',
      currentAgentId: 'agent-a',
      id: 'drift-3',
    });
    (d as unknown as Record<string, unknown>).triggerInput =
      'the agent has called search three times';

    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(50),
        payload: { case: 'driftDetected', value: d },
      }),
      store,
      0,
    );

    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans as never);
    expect(spans[0]?.attributes['drift.trigger_input']).toEqual({
      kind: 'string',
      value: 'the agent has called search three times',
    });
  });

  it('forward-compat: reasoningJudgeInvoked (unknown oneof case pre-merge) is tolerated', () => {
    // Pre-merge stubs don't know this case. Build the event manually so
    // the oneof `case` string is 'reasoningJudgeInvoked' and verify that
    // applyGoldfiveEvent either handles it (post-merge) or simply no-ops
    // (pre-merge) without crashing.
    const event = create(EventSchema, {
      eventId: 'ev',
      runId: 'run-1',
      sequence: 0n,
      emittedAt: ts(20),
    });
    (event as unknown as Record<string, unknown>).payload = {
      case: 'reasoningJudgeInvoked',
      value: {
        verdict: 'drift',
        severity: 'warning',
        reasoning: 'agent paraphrased the last turn',
        currentAgentId: 'agent-a',
        currentTaskId: 't1',
      },
    };

    expect(() => applyGoldfiveEvent(event, store, 0)).not.toThrow();

    // Post-merge path: if the switch case ran, a judge span landed.
    // Pre-merge path: nothing happens (no assertion on span count, but
    // the synthetic goldfive actor row is created on the judge branch).
    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans as never);
    // If the branch ran (which it does — the switch uses the string
    // compare directly), exactly one judge span lands.
    const judge = spans.find((s) => s && s.name.startsWith('judge:'));
    expect(judge).toBeTruthy();
    expect(judge?.attributes['judge.verdict']).toEqual({
      kind: 'string',
      value: 'drift',
    });
    expect(judge?.attributes['judge.target_agent_id']).toEqual({
      kind: 'string',
      value: 'agent-a',
    });
  });
});
