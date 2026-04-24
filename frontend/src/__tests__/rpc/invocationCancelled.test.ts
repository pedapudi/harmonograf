import { beforeEach, describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { SessionStore } from '../../gantt/index';
import { applyInvocationCancelled } from '../../rpc/goldfiveEvent';
import { InvocationCancelledSchema } from '../../pb/harmonograf/v1/telemetry_pb';
import { deriveInterventionsFromStore } from '../../lib/interventions';

// Tests the WatchSession dispatch for the new
// ``SessionUpdate.invocation_cancelled`` oneof variant (goldfive#251
// Stream C). Covers: record ingestion onto the session store,
// session-relative-ms derivation from emitted_at, agent row
// auto-registration so the Gantt has a lane to render on, synthesized
// cancel marker span, and intervention-row derivation.

function mkCancelPb(over: Partial<{
  runId: string;
  sequence: bigint;
  sessionId: string;
  invocationId: string;
  agentName: string;
  reason: string;
  severity: string;
  driftId: string;
  driftKind: string;
  detail: string;
  toolName: string;
  emittedAtSecs: number;
  emittedAtNanos: number;
}> = {}) {
  const emittedAt =
    over.emittedAtSecs != null
      ? create(TimestampSchema, {
          seconds: BigInt(over.emittedAtSecs),
          nanos: over.emittedAtNanos ?? 0,
        })
      : undefined;
  return create(InvocationCancelledSchema, {
    runId: over.runId ?? 'run-1',
    sequence: over.sequence ?? 5n,
    sessionId: over.sessionId ?? 'sess-c',
    invocationId: over.invocationId ?? 'inv-42',
    agentName:
      over.agentName ?? 'presentation-orchestrated-abc:researcher_agent',
    reason: over.reason ?? 'drift',
    severity: over.severity ?? 'critical',
    driftId: over.driftId ?? 'drift-uuid-1',
    driftKind: over.driftKind ?? 'off_topic',
    detail: over.detail ?? 'assistant veered off task',
    toolName: over.toolName ?? '',
    emittedAt,
  });
}

describe('applyInvocationCancelled', () => {
  let store: SessionStore;

  beforeEach(() => {
    store = new SessionStore();
  });

  it('appends an InvocationCancelRecord to the store', () => {
    const pb = mkCancelPb({ emittedAtSecs: 1000, emittedAtNanos: 0 });
    applyInvocationCancelled(pb, store, 0, 'sess-c');
    const list = store.invocationCancels.list();
    expect(list).toHaveLength(1);
    const r = list[0];
    expect(r.runId).toBe('run-1');
    expect(r.invocationId).toBe('inv-42');
    expect(r.agentId).toBe(
      'presentation-orchestrated-abc:researcher_agent',
    );
    expect(r.reason).toBe('drift');
    expect(r.severity).toBe('critical');
    expect(r.driftId).toBe('drift-uuid-1');
    expect(r.driftKind).toBe('off_topic');
    expect(r.detail).toBe('assistant veered off task');
    expect(r.toolName).toBe('');
  });

  it('derives session-relative ms from emitted_at', () => {
    const pb = mkCancelPb({ emittedAtSecs: 1_005, emittedAtNanos: 0 });
    // Session started at 1000s → 1_000_000ms
    applyInvocationCancelled(pb, store, 1_000_000, 'sess-c');
    const r = store.invocationCancels.list()[0];
    expect(r.recordedAtMs).toBe(5_000);
    expect(r.recordedAtAbsoluteMs).toBe(1_005_000);
  });

  it('auto-registers the cancelled agent row so the Gantt has a lane', () => {
    const pb = mkCancelPb({
      agentName: 'presentation-orchestrated-abc:stale_agent',
    });
    applyInvocationCancelled(pb, store, 0, 'sess-c');
    const agent = store.agents.get(
      'presentation-orchestrated-abc:stale_agent',
    );
    expect(agent).toBeTruthy();
    expect(agent?.name).toBe('stale_agent');
  });

  it('synthesizes a cancel marker span on the cancelled agent lane', () => {
    const pb = mkCancelPb({
      emittedAtSecs: 1_002,
      emittedAtNanos: 0,
      reason: 'drift',
      driftKind: 'off_topic',
    });
    applyInvocationCancelled(pb, store, 1_000_000, 'sess-c');
    const spans = store.spans.queryAgent(
      'presentation-orchestrated-abc:researcher_agent',
      0,
      Number.POSITIVE_INFINITY,
    );
    expect(spans).toHaveLength(1);
    const span = spans[0];
    expect(span.name).toBe('cancelled: drift');
    expect(span.attributes['harmonograf.cancel_marker']).toMatchObject({
      kind: 'bool',
      value: true,
    });
    expect(span.attributes['cancel.reason']).toMatchObject({
      kind: 'string',
      value: 'drift',
    });
    expect(span.attributes['cancel.drift_id']).toMatchObject({
      kind: 'string',
      value: 'drift-uuid-1',
    });
    expect(span.startMs).toBe(2_000);
  });

  it('does not produce a cancel span when agent_name is empty', () => {
    const pb = mkCancelPb({ agentName: '' });
    applyInvocationCancelled(pb, store, 0, 'sess-c');
    const count = Array.from(store.spans.all()).length;
    expect(count).toBe(0);
  });

  it('tool_name rides through when set', () => {
    const pb = mkCancelPb({ toolName: 'search_web' });
    applyInvocationCancelled(pb, store, 0, 'sess-c');
    const r = store.invocationCancels.list()[0];
    expect(r.toolName).toBe('search_web');
    const spans = store.spans.queryAgent(
      'presentation-orchestrated-abc:researcher_agent',
      0,
      Number.POSITIVE_INFINITY,
    );
    expect(spans[0].attributes['cancel.tool_name']).toMatchObject({
      kind: 'string',
      value: 'search_web',
    });
  });

  it('produces an InterventionRow with source=cancel via the deriver', () => {
    const pb = mkCancelPb({ emittedAtSecs: 1_010 });
    applyInvocationCancelled(pb, store, 1_000_000, 'sess-c');
    const rows = deriveInterventionsFromStore(store, []);
    expect(rows).toHaveLength(1);
    const row = rows[0];
    expect(row.source).toBe('cancel');
    expect(row.kind).toBe('CANCELLED');
    expect(row.severity).toBe('critical');
    expect(row.driftId).toBe('drift-uuid-1');
    expect(row.targetAgentId).toBe(
      'presentation-orchestrated-abc:researcher_agent',
    );
    expect(row.atMs).toBe(10_000);
    expect(row.bodyOrReason).toBe('assistant veered off task');
    // triggerEventId stays empty — cancel rows do NOT merge into their
    // triggering drift row; both coexist on the intervention list.
    expect(row.triggerEventId).toBe('');
  });

  it('derives a default body when detail is empty', () => {
    const pb = mkCancelPb({ detail: '' });
    applyInvocationCancelled(pb, store, 0, 'sess-c');
    const [row] = deriveInterventionsFromStore(store, []);
    expect(row.bodyOrReason).toBe('cancelled (drift → off_topic)');
  });

  it('coexists with its triggering DriftRecord in the intervention list', () => {
    // Append a drift with matching drift_id.
    store.drifts.append({
      kind: 'off_topic',
      severity: 'critical',
      detail: 'model veered',
      taskId: 't1',
      agentId: 'presentation-orchestrated-abc:researcher_agent',
      recordedAtMs: 9_000,
      annotationId: '',
      driftId: 'drift-uuid-1',
    });
    const pb = mkCancelPb({ emittedAtSecs: 1_010 });
    applyInvocationCancelled(pb, store, 1_000_000, 'sess-c');
    const rows = deriveInterventionsFromStore(store, []);
    // Both the drift row AND the cancel row appear — the cancel is
    // deliberately NOT merged into the drift (they represent different
    // facets: the drift is WHY, the cancel is WHAT happened).
    const sources = rows.map((r) => r.source);
    expect(sources).toContain('drift');
    expect(sources).toContain('cancel');
    const cancelRow = rows.find((r) => r.source === 'cancel')!;
    // Backlink is preserved on the cancel row for hover / click-through
    // to the drift detail drawer.
    expect(cancelRow.driftId).toBe('drift-uuid-1');
  });
});
