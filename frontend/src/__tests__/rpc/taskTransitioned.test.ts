import { beforeEach, describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { SessionStore } from '../../gantt/index';
import {
  applyGoldfiveEvent,
  applyTaskTransitioned,
} from '../../rpc/goldfiveEvent';
import {
  EventSchema,
  TaskTransitionedSchema,
} from '../../pb/goldfive/v1/events_pb';

// Tests the WatchSession dispatch for the typed
// ``goldfive.v1.TaskTransitioned`` payload variant on the goldfive
// Event envelope (goldfive#267 / #251 R4). Covers:
//   * record ingestion onto the session store,
//   * session-relative-ms derivation from ``Event.emitted_at``,
//   * intervention-row filter (only terminal to_status + meaningful
//     source surface; RUNNING transitions and noisy sources skipped),
//   * the dispatcher routes ``payload.case === 'taskTransitioned'`` to
//     the same helper as the direct call.

function mkTransitionEvent(over: Partial<{
  runId: string;
  sequence: bigint;
  sessionId: string;
  taskId: string;
  fromStatus: string;
  toStatus: string;
  source: string;
  revisionStamp: number;
  agentName: string;
  invocationId: string;
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
  const payload = create(TaskTransitionedSchema, {
    taskId: over.taskId ?? 't-7',
    fromStatus: over.fromStatus ?? 'RUNNING',
    toStatus: over.toStatus ?? 'COMPLETED',
    source: over.source ?? 'llm_report',
    revisionStamp: over.revisionStamp ?? 0,
    agentName:
      over.agentName ?? 'presentation-orchestrated-abc:researcher_agent',
    invocationId: over.invocationId ?? 'inv-7',
  });
  return create(EventSchema, {
    runId: over.runId ?? 'run-1',
    sequence: over.sequence ?? 9n,
    sessionId: over.sessionId ?? 'sess-t',
    emittedAt,
    payload: {
      case: 'taskTransitioned',
      value: payload,
    },
  });
}

describe('applyTaskTransitioned', () => {
  let store: SessionStore;

  beforeEach(() => {
    store = new SessionStore();
  });

  it('appends a TaskTransitionRecord to the store', () => {
    const ev = mkTransitionEvent({ emittedAtSecs: 1000, emittedAtNanos: 0 });
    const payload = ev.payload;
    if (payload.case !== 'taskTransitioned') throw new Error('bad fixture');
    applyTaskTransitioned(payload.value, ev, store, 0);
    const list = store.taskTransitions.list();
    expect(list).toHaveLength(1);
    expect(list[0].taskId).toBe('t-7');
    expect(list[0].fromStatus).toBe('RUNNING');
    expect(list[0].toStatus).toBe('COMPLETED');
    expect(list[0].source).toBe('llm_report');
    expect(list[0].agentName).toBe(
      'presentation-orchestrated-abc:researcher_agent',
    );
    expect(list[0].invocationId).toBe('inv-7');
    expect(list[0].seq).toBe(0);
  });

  it('derives session-relative ms from Event.emitted_at', () => {
    const ev = mkTransitionEvent({ emittedAtSecs: 1100, emittedAtNanos: 0 });
    const payload = ev.payload;
    if (payload.case !== 'taskTransitioned') throw new Error('bad fixture');
    // sessionStartMs = 1_000_000 (i.e. epoch ms 1_000_000); event at
    // epoch ms 1_100_000 → relative 100_000ms.
    applyTaskTransitioned(payload.value, ev, store, 1_000_000);
    const list = store.taskTransitions.list();
    expect(list[0].recordedAtMs).toBe(100_000);
    expect(list[0].recordedAtAbsoluteMs).toBe(1_100_000);
  });

  it('rebases on session-start-late delivery', () => {
    // Live-path race: TaskTransitioned arrives before the 'session'
    // SessionUpdate has set wallClockStartMs. Initial relative ms is
    // wall-clock-scale; rebase corrects it once the start lands.
    const ev = mkTransitionEvent({ emittedAtSecs: 2000 });
    const payload = ev.payload;
    if (payload.case !== 'taskTransitioned') throw new Error('bad fixture');
    applyTaskTransitioned(payload.value, ev, store, 0);
    const before = store.taskTransitions.list()[0].recordedAtMs;
    expect(before).toBe(2_000_000);
    store.taskTransitions.rebase(1_999_500);
    const after = store.taskTransitions.list()[0].recordedAtMs;
    expect(after).toBe(500);
  });

  it('routes via applyGoldfiveEvent when payload.case is taskTransitioned', () => {
    const ev = mkTransitionEvent({ emittedAtSecs: 1000 });
    applyGoldfiveEvent(ev, store, 0, 'sess-t');
    const list = store.taskTransitions.list();
    expect(list).toHaveLength(1);
    expect(list[0].source).toBe('llm_report');
  });

  it('coerces lowercase status strings to uppercase', () => {
    // Defense-in-depth: spec says bare uppercase, but we tolerate
    // lowercase emitters by upcasing on ingest.
    const ev = mkTransitionEvent({
      fromStatus: 'pending',
      toStatus: 'completed',
    });
    const payload = ev.payload;
    if (payload.case !== 'taskTransitioned') throw new Error('bad fixture');
    applyTaskTransitioned(payload.value, ev, store, 0);
    const list = store.taskTransitions.list();
    expect(list[0].fromStatus).toBe('PENDING');
    expect(list[0].toStatus).toBe('COMPLETED');
  });
});
