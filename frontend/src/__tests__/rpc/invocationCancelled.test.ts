import { beforeEach, describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { SessionStore } from '../../gantt/index';
import { applyGoldfiveEvent, applyInvocationCancelled } from '../../rpc/goldfiveEvent';
import {
  EventSchema,
  InvocationCancelledSchema,
} from '../../pb/goldfive/v1/events_pb';
import { deriveInterventionsFromStore } from '../../lib/interventions';

// Tests the WatchSession dispatch for the typed
// ``goldfive.v1.InvocationCancelled`` payload variant on the goldfive
// Event envelope (goldfive#262 / harmonograf Wave 2 / A8). Covers:
// record ingestion onto the session store, session-relative-ms
// derivation from ``Event.emitted_at``, agent row auto-registration so
// the Gantt has a lane to render on, the synthesized cancel marker
// span, intervention-row derivation, and that the dispatcher routes
// ``payload.case === 'invocationCancelled'`` to the same helper as the
// direct call (regression for the case where the cancel arrives on a
// goldfive Event rather than via a dedicated SessionUpdate slot).

function mkCancelEvent(over: Partial<{
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
  const payload = create(InvocationCancelledSchema, {
    invocationId: over.invocationId ?? 'inv-42',
    agentName:
      over.agentName ?? 'presentation-orchestrated-abc:researcher_agent',
    reason: over.reason ?? 'drift',
    severity: over.severity ?? 'critical',
    driftId: over.driftId ?? 'drift-uuid-1',
    driftKind: over.driftKind ?? 'off_topic',
    detail: over.detail ?? 'assistant veered off task',
    toolName: over.toolName ?? '',
  });
  return create(EventSchema, {
    runId: over.runId ?? 'run-1',
    sequence: over.sequence ?? 5n,
    sessionId: over.sessionId ?? 'sess-c',
    emittedAt,
    payload: {
      case: 'invocationCancelled',
      value: payload,
    },
  });
}

describe('applyInvocationCancelled', () => {
  let store: SessionStore;

  beforeEach(() => {
    store = new SessionStore();
  });

  it('appends an InvocationCancelRecord to the store', () => {
    const ev = mkCancelEvent({ emittedAtSecs: 1000, emittedAtNanos: 0 });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    applyInvocationCancelled(payload.value, ev, store, 0, 'sess-c');
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

  it('derives session-relative ms from Event.emitted_at', () => {
    const ev = mkCancelEvent({ emittedAtSecs: 1_005, emittedAtNanos: 0 });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    // Session started at 1000s → 1_000_000ms
    applyInvocationCancelled(payload.value, ev, store, 1_000_000, 'sess-c');
    const r = store.invocationCancels.list()[0];
    expect(r.recordedAtMs).toBe(5_000);
    expect(r.recordedAtAbsoluteMs).toBe(1_005_000);
  });

  it('auto-registers the cancelled agent row so the Gantt has a lane', () => {
    const ev = mkCancelEvent({
      agentName: 'presentation-orchestrated-abc:stale_agent',
    });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    applyInvocationCancelled(payload.value, ev, store, 0, 'sess-c');
    const agent = store.agents.get(
      'presentation-orchestrated-abc:stale_agent',
    );
    expect(agent).toBeTruthy();
    expect(agent?.name).toBe('stale_agent');
  });

  it('synthesizes a cancel marker span on the cancelled agent lane', () => {
    const ev = mkCancelEvent({
      emittedAtSecs: 1_002,
      emittedAtNanos: 0,
      reason: 'drift',
      driftKind: 'off_topic',
    });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    applyInvocationCancelled(payload.value, ev, store, 1_000_000, 'sess-c');
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
    const ev = mkCancelEvent({ agentName: '' });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    applyInvocationCancelled(payload.value, ev, store, 0, 'sess-c');
    const count = Array.from(store.spans.all()).length;
    expect(count).toBe(0);
  });

  it('tool_name rides through when set', () => {
    const ev = mkCancelEvent({ toolName: 'search_web' });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    applyInvocationCancelled(payload.value, ev, store, 0, 'sess-c');
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
    const ev = mkCancelEvent({ emittedAtSecs: 1_010 });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    applyInvocationCancelled(payload.value, ev, store, 1_000_000, 'sess-c');
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
    const ev = mkCancelEvent({ detail: '' });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    applyInvocationCancelled(payload.value, ev, store, 0, 'sess-c');
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
    const ev = mkCancelEvent({ emittedAtSecs: 1_010 });
    const payload = ev.payload;
    if (payload.case !== 'invocationCancelled') throw new Error('bad fixture');
    applyInvocationCancelled(payload.value, ev, store, 1_000_000, 'sess-c');
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

describe('applyGoldfiveEvent invocationCancelled dispatch', () => {
  let store: SessionStore;

  beforeEach(() => {
    store = new SessionStore();
  });

  it('routes invocationCancelled payloads to the cancel pipeline', () => {
    // Migration regression: post-#262, the cancel arrives as a
    // ``goldfive.v1.Event`` with ``payload.case === 'invocationCancelled'``
    // — NOT on a dedicated ``SessionUpdate.invocation_cancelled`` slot.
    // applyGoldfiveEvent must fan the payload into applyInvocationCancelled
    // so the store gets the same record + synthesized span as the
    // pre-migration dedicated dispatch produced.
    const ev = mkCancelEvent({ emittedAtSecs: 1_010 });
    applyGoldfiveEvent(ev, store, 1_000_000, 'sess-c');
    const list = store.invocationCancels.list();
    expect(list).toHaveLength(1);
    expect(list[0].invocationId).toBe('inv-42');
    expect(list[0].recordedAtMs).toBe(10_000);
    // Cancel marker span synthesized on the agent row.
    const spans = store.spans.queryAgent(
      'presentation-orchestrated-abc:researcher_agent',
      0,
      Number.POSITIVE_INFINITY,
    );
    expect(spans).toHaveLength(1);
    expect(spans[0].name).toBe('cancelled: drift');
  });
});
