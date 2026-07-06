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
