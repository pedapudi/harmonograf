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

  it('Option X: reasoningJudgeInvoked is dropped client-side (sink translates to a span)', () => {
    // Under Option X the harmonograf client sink translates
    // goldfive_llm_call_{start,end} and reasoning_judge_invoked events
    // into SpanStart/SpanEnd frames on the span transport. If a stale
    // ReasoningJudgeInvoked reaches the frontend via the goldfive-event
    // channel (e.g. an old-format replay), the handler no-ops — no
    // synthetic span, no actor row mutation.
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

    const spans: Array<ReturnType<typeof store.spans.get>> = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans as never);
    expect(spans).toHaveLength(0);
  });

  // harmonograf#goldfive-unify: the sink (PR #159) ships goldfive LLM / judge
  // spans on a compound `<client>:goldfive` agent id. Legacy frontend
  // synthesizers used `__goldfive__`. Without unification the Graph view
  // rendered goldfive twice. These tests pin the single-row invariant in
  // both arrival orders (sink-first, synth-first).
  describe('goldfive actor alias', () => {
    function seedCompoundGoldfiveSpan(s: SessionStore, id: string) {
      // A minimal sink-translated span on the compound id. We don't go
      // through convertSpan — the test is asserting the alias merge, not
      // the proto boundary.
      s.spans.append({
        id,
        sessionId: 'sess-1',
        agentId: 'client-42:goldfive',
        parentSpanId: null,
        kind: 'LLM_CALL',
        status: 'COMPLETED',
        name: 'judge_reasoning',
        startMs: 100,
        endMs: 110,
        links: [],
        attributes: {
          'goldfive.call_name': { kind: 'string', value: 'judge_reasoning' },
        },
        payloadRefs: [],
        error: null,
        lane: -1,
        replaced: false,
      });
      s.agents.upsert({
        id: 'client-42:goldfive',
        name: 'goldfive',
        framework: 'UNKNOWN',
        capabilities: [],
        status: 'CONNECTED',
        connectedAtMs: 100,
        currentActivity: '',
        stuck: false,
        taskReport: '',
        taskReportAt: 0,
        metadata: {},
      });
    }

    it('sink-first: a DriftDetected synthesized AFTER a compound :goldfive row lands on that row (no __goldfive__ duplicate)', () => {
      seedCompoundGoldfiveSpan(store, 'judge-1');
      applyGoldfiveEvent(
        create(EventSchema, {
          eventId: 'ev',
          runId: 'run-1',
          sequence: 1n,
          emittedAt: ts(30),
          payload: {
            case: 'driftDetected',
            value: create(DriftDetectedSchema, {
              kind: DriftKind.LOOPING_REASONING,
              severity: DriftSeverity.WARNING,
              detail: 'loop',
              currentTaskId: 't1',
              currentAgentId: 'agent-a',
              id: 'drift-1',
            }),
          },
        }),
        store,
        0,
      );
      // Only ONE goldfive row exists — the compound one.
      expect(store.agents.get('client-42:goldfive')).toBeTruthy();
      expect(store.agents.get(GOLDFIVE_ACTOR_ID)).toBeFalsy();
      // The drift span landed on the compound row, NOT on __goldfive__.
      const onCompound: Array<ReturnType<typeof store.spans.get>> = [];
      store.spans.queryAgent('client-42:goldfive', 0, 1_000_000, onCompound as never);
      expect(onCompound.map((s) => s?.name)).toContain('looping_reasoning');
      const onLegacy: Array<ReturnType<typeof store.spans.get>> = [];
      store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, onLegacy as never);
      expect(onLegacy).toHaveLength(0);
    });

    it('synth-first: calling mergeGoldfiveAlias AFTER a DriftDetected has seeded __goldfive__ moves spans onto the compound row', () => {
      // Drift arrives first — synthesizer creates __goldfive__ + a drift span.
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
              detail: 'loop',
              currentTaskId: 't1',
              currentAgentId: 'agent-a',
              id: 'drift-1',
            }),
          },
        }),
        store,
        0,
      );
      expect(store.agents.get(GOLDFIVE_ACTOR_ID)).toBeTruthy();
      // Sink span lands later — call mergeGoldfiveAlias (what hooks.ts does).
      seedCompoundGoldfiveSpan(store, 'judge-1');
      const canonical = store.mergeGoldfiveAlias();
      expect(canonical).toBe('client-42:goldfive');
      expect(store.agents.get(GOLDFIVE_ACTOR_ID)).toBeFalsy();
      // The original synthesized drift span now lives on the compound row.
      const onCompound: Array<ReturnType<typeof store.spans.get>> = [];
      store.spans.queryAgent('client-42:goldfive', 0, 1_000_000, onCompound as never);
      expect(onCompound.map((s) => s?.name)).toEqual(
        expect.arrayContaining(['looping_reasoning', 'judge_reasoning']),
      );
    });

    it('resolveGoldfiveActorId returns the compound id when present, legacy otherwise', () => {
      expect(store.resolveGoldfiveActorId()).toBe(GOLDFIVE_ACTOR_ID);
      seedCompoundGoldfiveSpan(store, 'judge-1');
      expect(store.resolveGoldfiveActorId()).toBe('client-42:goldfive');
    });

    it('mergeGoldfiveAlias is idempotent and safe when only __goldfive__ exists', () => {
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
              detail: 'loop',
              currentTaskId: 't1',
              currentAgentId: 'agent-a',
              id: 'drift-1',
            }),
          },
        }),
        store,
        0,
      );
      // No compound row yet — merge is a no-op and returns the legacy id.
      expect(store.mergeGoldfiveAlias()).toBe(GOLDFIVE_ACTOR_ID);
      expect(store.agents.get(GOLDFIVE_ACTOR_ID)).toBeTruthy();
      // Double-call after a later compound arrival is idempotent.
      seedCompoundGoldfiveSpan(store, 'judge-1');
      expect(store.mergeGoldfiveAlias()).toBe('client-42:goldfive');
      expect(store.mergeGoldfiveAlias()).toBe('client-42:goldfive');
      expect(store.agents.get(GOLDFIVE_ACTOR_ID)).toBeFalsy();
    });
  });
});
