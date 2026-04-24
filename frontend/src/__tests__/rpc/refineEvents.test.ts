import { beforeEach, describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { SessionStore } from '../../gantt/index';
import {
  applyRefineAttempted,
  applyRefineFailed,
} from '../../rpc/goldfiveEvent';
import {
  RefineAttemptedSchema,
  RefineFailedSchema,
} from '../../pb/harmonograf/v1/telemetry_pb';
import { deriveInterventionsFromStore } from '../../lib/interventions';
import type { TaskPlan } from '../../gantt/types';

// Tests the WatchSession dispatch for the new RefineAttempted /
// RefineFailed oneof variants (goldfive#264). Coverage:
//   * record ingestion onto the session store (separate registries for
//     attempts vs failures),
//   * agent-id passthrough (the sink already canonicalized),
//   * synthesized failed-refine marker span on the goldfive lane,
//   * intervention-row derivation: attempt + plan_revised → success
//     row; attempt + RefineFailed → warning row; orphan attempted →
//     pending row.
//   * Orphan failure (failed without an attempt) is gracefully ignored.

function mkAttemptedPb(
  over: Partial<{
    runId: string;
    sequence: bigint;
    sessionId: string;
    attemptId: string;
    driftId: string;
    triggerKind: string;
    triggerSeverity: string;
    currentTaskId: string;
    currentAgentId: string;
    emittedAtSecs: number;
    emittedAtNanos: number;
  }> = {},
) {
  const emittedAt =
    over.emittedAtSecs != null
      ? create(TimestampSchema, {
          seconds: BigInt(over.emittedAtSecs),
          nanos: over.emittedAtNanos ?? 0,
        })
      : undefined;
  return create(RefineAttemptedSchema, {
    runId: over.runId ?? 'run-1',
    sequence: over.sequence ?? 11n,
    sessionId: over.sessionId ?? 'sess-r',
    attemptId: over.attemptId ?? 'att-uuid-1',
    driftId: over.driftId ?? 'drift-uuid-1',
    triggerKind: over.triggerKind ?? 'looping_reasoning',
    triggerSeverity: over.triggerSeverity ?? 'warning',
    currentTaskId: over.currentTaskId ?? 'task-7',
    currentAgentId:
      over.currentAgentId ?? 'presentation-orchestrated-abc:researcher_agent',
    emittedAt,
  });
}

function mkFailedPb(
  over: Partial<{
    runId: string;
    sequence: bigint;
    sessionId: string;
    attemptId: string;
    driftId: string;
    triggerKind: string;
    triggerSeverity: string;
    failureKind: string;
    reason: string;
    detail: string;
    currentTaskId: string;
    currentAgentId: string;
    emittedAtSecs: number;
    emittedAtNanos: number;
  }> = {},
) {
  const emittedAt =
    over.emittedAtSecs != null
      ? create(TimestampSchema, {
          seconds: BigInt(over.emittedAtSecs),
          nanos: over.emittedAtNanos ?? 0,
        })
      : undefined;
  return create(RefineFailedSchema, {
    runId: over.runId ?? 'run-1',
    sequence: over.sequence ?? 12n,
    sessionId: over.sessionId ?? 'sess-r',
    attemptId: over.attemptId ?? 'att-uuid-1',
    driftId: over.driftId ?? 'drift-uuid-1',
    triggerKind: over.triggerKind ?? 'looping_reasoning',
    triggerSeverity: over.triggerSeverity ?? 'warning',
    failureKind: over.failureKind ?? 'validator_rejected',
    reason: over.reason ?? 'supersedes coverage missing',
    detail: over.detail ?? 'task t1 superseded but no replacement',
    currentTaskId: over.currentTaskId ?? 'task-7',
    currentAgentId:
      over.currentAgentId ?? 'presentation-orchestrated-abc:researcher_agent',
    emittedAt,
  });
}

function mkPlan(
  over: Partial<TaskPlan> & { id: string },
): TaskPlan {
  return {
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 1000,
    summary: '',
    tasks: [],
    edges: [],
    revisionReason: '',
    revisionKind: '',
    revisionSeverity: '',
    revisionIndex: 0,
    triggerEventId: '',
    ...over,
  };
}

describe('applyRefineAttempted', () => {
  let store: SessionStore;

  beforeEach(() => {
    store = new SessionStore();
  });

  it('appends a RefineAttemptRecord to the store', () => {
    const pb = mkAttemptedPb({ emittedAtSecs: 1000, emittedAtNanos: 0 });
    applyRefineAttempted(pb, store, 0);
    const list = store.refineAttempts.list();
    expect(list).toHaveLength(1);
    const r = list[0];
    expect(r.runId).toBe('run-1');
    expect(r.attemptId).toBe('att-uuid-1');
    expect(r.driftId).toBe('drift-uuid-1');
    expect(r.triggerKind).toBe('looping_reasoning');
    expect(r.triggerSeverity).toBe('warning');
    expect(r.taskId).toBe('task-7');
    expect(r.agentId).toBe(
      'presentation-orchestrated-abc:researcher_agent',
    );
    // recordedAtMs = (1000 * 1000) - 0 (sessionStartMs).
    expect(r.recordedAtMs).toBe(1_000_000);
    expect(r.recordedAtAbsoluteMs).toBe(1_000_000);
  });

  it('does NOT synthesize a span — successful path mints one on plan_revised', () => {
    applyRefineAttempted(mkAttemptedPb({ emittedAtSecs: 1000 }), store, 0);
    expect(Array.from(store.spans.all())).toHaveLength(0);
  });

  it('dedups duplicate attemptIds (initial-burst replay race)', () => {
    const pb = mkAttemptedPb({ emittedAtSecs: 1000 });
    applyRefineAttempted(pb, store, 0);
    applyRefineAttempted(pb, store, 0);
    expect(store.refineAttempts.list()).toHaveLength(1);
  });

  it('lowercases triggerKind / triggerSeverity for downstream comparison', () => {
    const pb = mkAttemptedPb({
      triggerKind: 'LOOPING_REASONING',
      triggerSeverity: 'WARNING',
      emittedAtSecs: 1000,
    });
    applyRefineAttempted(pb, store, 0);
    const r = store.refineAttempts.list()[0];
    expect(r.triggerKind).toBe('looping_reasoning');
    expect(r.triggerSeverity).toBe('warning');
  });

  it('falls back to Date.now when emittedAt is unset', () => {
    const before = Date.now();
    applyRefineAttempted(mkAttemptedPb({ /* no emittedAt */ }), store, 0);
    const after = Date.now();
    const r = store.refineAttempts.list()[0];
    expect(r.recordedAtAbsoluteMs).toBeGreaterThanOrEqual(before);
    expect(r.recordedAtAbsoluteMs).toBeLessThanOrEqual(after);
  });
});

describe('applyRefineFailed', () => {
  let store: SessionStore;

  beforeEach(() => {
    store = new SessionStore();
  });

  it('appends a RefineFailureRecord to the store', () => {
    applyRefineFailed(
      mkFailedPb({ emittedAtSecs: 1010, emittedAtNanos: 500_000_000 }),
      store,
      0,
      'sess-r',
    );
    const list = store.refineFailures.list();
    expect(list).toHaveLength(1);
    const r = list[0];
    expect(r.attemptId).toBe('att-uuid-1');
    expect(r.failureKind).toBe('validator_rejected');
    expect(r.reason).toBe('supersedes coverage missing');
    expect(r.detail).toBe('task t1 superseded but no replacement');
  });

  it('synthesizes a failed-refine marker span on the goldfive lane', () => {
    applyRefineFailed(
      mkFailedPb({ emittedAtSecs: 1010 }),
      store,
      0,
      'sess-r',
    );
    const spans = Array.from(store.spans.all());
    expect(spans).toHaveLength(1);
    const s = spans[0];
    expect(s.name).toBe('refine failed: validator_rejected');
    expect(s.attributes['harmonograf.refine_failed']).toEqual({
      kind: 'bool',
      value: true,
    });
    expect(s.attributes['refine.attempt_id']).toEqual({
      kind: 'string',
      value: 'att-uuid-1',
    });
    expect(s.attributes['refine.failure_kind']).toEqual({
      kind: 'string',
      value: 'validator_rejected',
    });
    // Lands on the goldfive synthetic actor row (legacy
    // ``__goldfive__`` constant when no compound :goldfive id has
    // landed yet — mergeGoldfiveAlias collapses them when one does).
    expect(s.agentId).toMatch(/goldfive(_*|$)/);
  });

  it('dedups duplicate attemptIds', () => {
    const pb = mkFailedPb({ emittedAtSecs: 1010 });
    applyRefineFailed(pb, store, 0, 'sess-r');
    applyRefineFailed(pb, store, 0, 'sess-r');
    expect(store.refineFailures.list()).toHaveLength(1);
    // Second emit shouldn't double-synthesize the marker span either —
    // the span id is keyed on attemptId so an idempotent append is fine,
    // but an upsert would still leave us with exactly one span row.
    expect(Array.from(store.spans.all())).toHaveLength(1);
  });
});

describe('intervention-row correlation (Option A merge)', () => {
  let store: SessionStore;

  beforeEach(() => {
    store = new SessionStore();
  });

  it('merges attempted + successful plan_revised into one REFINE row', () => {
    applyRefineAttempted(
      mkAttemptedPb({ emittedAtSecs: 1000, attemptId: 'att-1' }),
      store,
      0,
    );
    // Simulate a successful refine: a plan rev whose triggerEventId
    // matches the attempted's drift_id.
    store.tasks.upsertPlan(
      mkPlan({
        id: 'plan-1',
        revisionIndex: 3,
        revisionReason: 'replaced looping task',
        revisionKind: 'looping_reasoning',
        triggerEventId: 'drift-uuid-1',
        createdAtMs: 1_500,
      }),
    );
    const rows = deriveInterventionsFromStore(store, []);
    const refineRow = rows.find((r) => r.source === 'refine');
    expect(refineRow).toBeDefined();
    expect(refineRow!.kind).toBe('REFINE:LOOPING_REASONING');
    expect(refineRow!.outcome).toBe('plan_revised:r3');
    expect(refineRow!.planRevisionIndex).toBe(3);
    expect(refineRow!.attemptId).toBe('att-1');
    expect(refineRow!.severity).toBe('warning');
    expect(refineRow!.failureKind).toBe('');
  });

  it('merges attempted + RefineFailed into one REFINE_FAILED warning row', () => {
    applyRefineAttempted(
      mkAttemptedPb({ emittedAtSecs: 1000, attemptId: 'att-2' }),
      store,
      0,
    );
    applyRefineFailed(
      mkFailedPb({
        emittedAtSecs: 1001,
        attemptId: 'att-2',
        failureKind: 'parse_error',
        reason: 'malformed JSON',
        detail: 'expected ] at column 84',
      }),
      store,
      0,
      'sess-r',
    );
    const rows = deriveInterventionsFromStore(store, []);
    const refineRow = rows.find((r) => r.source === 'refine');
    expect(refineRow).toBeDefined();
    expect(refineRow!.kind).toBe('REFINE_FAILED:PARSE_ERROR');
    expect(refineRow!.outcome).toBe('refine_failed:parse_error');
    expect(refineRow!.severity).toBe('warning');
    expect(refineRow!.failureKind).toBe('parse_error');
    // Body prefers the detail field.
    expect(refineRow!.bodyOrReason).toBe('expected ] at column 84');
  });

  it('renders an attempted with no terminal as a pending row', () => {
    applyRefineAttempted(
      mkAttemptedPb({ emittedAtSecs: 1000, attemptId: 'att-pending' }),
      store,
      0,
    );
    const rows = deriveInterventionsFromStore(store, []);
    const refineRow = rows.find((r) => r.source === 'refine');
    expect(refineRow).toBeDefined();
    expect(refineRow!.outcome).toBe('pending');
    expect(refineRow!.kind).toBe('REFINE:LOOPING_REASONING');
    expect(refineRow!.failureKind).toBe('');
  });

  it('failure overrides success when both are present (defensive)', () => {
    // This is a corruption/race scenario — goldfive should never emit
    // both for the same attempt. The deriver picks failure since
    // ``failure`` correlates by attempt_id (strict) while success
    // correlates by drift_id (which can match unrelated plan revs in
    // weird histories).
    applyRefineAttempted(
      mkAttemptedPb({ emittedAtSecs: 1000, attemptId: 'att-conflict' }),
      store,
      0,
    );
    store.tasks.upsertPlan(
      mkPlan({
        id: 'plan-x',
        revisionIndex: 2,
        revisionKind: 'looping_reasoning',
        triggerEventId: 'drift-uuid-1',
        createdAtMs: 1_500,
      }),
    );
    applyRefineFailed(
      mkFailedPb({
        emittedAtSecs: 1001,
        attemptId: 'att-conflict',
        failureKind: 'llm_error',
      }),
      store,
      0,
      'sess-r',
    );
    const rows = deriveInterventionsFromStore(store, []);
    const refineRow = rows.find((r) => r.source === 'refine');
    expect(refineRow!.kind).toBe('REFINE_FAILED:LLM_ERROR');
    expect(refineRow!.outcome).toBe('refine_failed:llm_error');
  });

  it('orphan RefineFailed without an attempted is dropped from the merged rows', () => {
    // The deriver iterates attempts, so an orphan failure produces no
    // row in the merged set (it's still on the failure registry for
    // debugging / direct lookup, but no operator-facing row).
    applyRefineFailed(
      mkFailedPb({
        emittedAtSecs: 1010,
        attemptId: 'att-orphan',
      }),
      store,
      0,
      'sess-r',
    );
    const rows = deriveInterventionsFromStore(store, []);
    expect(rows.find((r) => r.source === 'refine')).toBeUndefined();
  });

  it('does not collapse the refine row into its source drift row', () => {
    // Both rows should coexist in the merged output: the drift row
    // captures the WHY (looping_reasoning detected) and the refine
    // row captures the orchestrator's response.
    store.drifts.append({
      kind: 'looping_reasoning',
      severity: 'warning',
      detail: 'agent looped',
      taskId: 'task-7',
      agentId: 'researcher_agent',
      recordedAtMs: 900,
      recordedAtAbsoluteMs: 900,
      annotationId: '',
      driftId: 'drift-uuid-1',
      authoredBy: '',
    });
    applyRefineAttempted(
      mkAttemptedPb({ emittedAtSecs: 1000, attemptId: 'att-1' }),
      store,
      0,
    );
    applyRefineFailed(
      mkFailedPb({ emittedAtSecs: 1001, attemptId: 'att-1' }),
      store,
      0,
      'sess-r',
    );
    const rows = deriveInterventionsFromStore(store, []);
    const driftRows = rows.filter((r) => r.source === 'drift');
    const refineRows = rows.filter((r) => r.source === 'refine');
    expect(driftRows).toHaveLength(1);
    expect(refineRows).toHaveLength(1);
  });

  it('multiple attempts on the same drift each produce their own refine row', () => {
    applyRefineAttempted(
      mkAttemptedPb({ emittedAtSecs: 1000, attemptId: 'att-1' }),
      store,
      0,
    );
    applyRefineAttempted(
      mkAttemptedPb({
        emittedAtSecs: 1100,
        attemptId: 'att-2',
        // Same drift_id, different attempt_id (operator re-triggered
        // refine via goldfive-steer for the same drift).
        driftId: 'drift-uuid-1',
      }),
      store,
      0,
    );
    applyRefineFailed(
      mkFailedPb({ emittedAtSecs: 1001, attemptId: 'att-1' }),
      store,
      0,
      'sess-r',
    );
    // attempt-2 succeeds with a plan_revised triggered by drift-uuid-1.
    store.tasks.upsertPlan(
      mkPlan({
        id: 'plan-ok',
        revisionIndex: 1,
        revisionKind: 'looping_reasoning',
        triggerEventId: 'drift-uuid-1',
        createdAtMs: 1_200,
      }),
    );
    const rows = deriveInterventionsFromStore(store, []);
    const refineRows = rows.filter((r) => r.source === 'refine');
    expect(refineRows).toHaveLength(2);
    const failedOne = refineRows.find((r) => r.attemptId === 'att-1');
    const successOne = refineRows.find((r) => r.attemptId === 'att-2');
    expect(failedOne!.outcome).toMatch(/^refine_failed:/);
    expect(successOne!.outcome).toBe('plan_revised:r1');
  });
});
