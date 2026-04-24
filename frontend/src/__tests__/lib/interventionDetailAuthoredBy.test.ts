// harmonograf#197 forward-compat — DriftDetected.authored_by surfacing.
//
// goldfive's /tmp/goldfive-steer-unify branch adds DriftDetected.authored_by
// ("user" / "goldfive" / ""). Until that branch merges + the submodule
// bumps, the field is absent from the generated stubs, so ingest reads it
// through `unknown` casts. This test verifies the field:
//
//   * lands on the DriftRecord when present on the wire
//   * lands on the synthesized drift span when present on the wire
//   * is surfaced by resolveDriftDetail.authoredBy
//   * is silently absent for legacy events (undefined on the field)
//
// The test uses the string-oneof-case + unknown-cast escape hatch to feed
// authored_by without needing the stub to know about it yet.

import { beforeEach, describe, expect, it } from 'vitest';
import { create } from '@bufbuild/protobuf';
import { TimestampSchema } from '@bufbuild/protobuf/wkt';
import { SessionStore } from '../../gantt/index';
import { applyGoldfiveEvent } from '../../rpc/goldfiveEvent';
import {
  EventSchema,
  DriftDetectedSchema,
} from '../../pb/goldfive/v1/events_pb';
import { DriftKind, DriftSeverity } from '../../pb/goldfive/v1/types_pb';
import { resolveDriftDetail } from '../../lib/interventionDetail';
import { GOLDFIVE_ACTOR_ID } from '../../theme/agentColors';
import type { Span } from '../../gantt/types';

function ts(seconds: number) {
  return create(TimestampSchema, { seconds: BigInt(seconds), nanos: 0 });
}

describe('DriftDetected.authored_by (forward-compat)', () => {
  let store: SessionStore;
  beforeEach(() => {
    store = new SessionStore();
  });

  it('carries authored_by="goldfive" onto the DriftRecord + span + detail', () => {
    const d = create(DriftDetectedSchema, {
      kind: DriftKind.LOOPING_REASONING,
      severity: DriftSeverity.WARNING,
      detail: 'loop detected',
      currentTaskId: 't1',
      currentAgentId: 'agent-a',
      id: 'drift-gf-1',
    });
    (d as unknown as Record<string, unknown>).authoredBy = 'goldfive';
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-gf',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(20),
        payload: { case: 'driftDetected', value: d },
      }),
      store,
      0,
    );

    const drifts = store.drifts.list();
    expect(drifts[0].authoredBy).toBe('goldfive');

    const spans: Span[] = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans);
    const driftSpan = spans.find((s) => s.name === 'looping_reasoning');
    expect(driftSpan?.attributes['drift.authored_by']).toEqual({
      kind: 'string',
      value: 'goldfive',
    });

    const detail = resolveDriftDetail(drifts[0], [], store);
    expect(detail.authoredBy).toBe('goldfive');
  });

  it('carries authored_by="user" through for user-control drifts', () => {
    const d = create(DriftDetectedSchema, {
      kind: DriftKind.USER_STEER,
      severity: DriftSeverity.INFO,
      detail: 'steer message',
      currentTaskId: 't1',
      currentAgentId: 'agent-a',
      id: 'drift-user-1',
    });
    (d as unknown as Record<string, unknown>).authoredBy = 'user';
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-user',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(21),
        payload: { case: 'driftDetected', value: d },
      }),
      store,
      0,
    );

    const drifts = store.drifts.list();
    const detail = resolveDriftDetail(drifts[0], [], store);
    expect(detail.authoredBy).toBe('user');
  });

  it('legacy events without authored_by leave the detail field empty', () => {
    const d = create(DriftDetectedSchema, {
      kind: DriftKind.LOOPING_REASONING,
      severity: DriftSeverity.WARNING,
      detail: 'loop',
      currentTaskId: 't1',
      currentAgentId: 'agent-a',
      id: 'drift-legacy-1',
      // No authoredBy stamped. Pre-merge wire.
    });
    applyGoldfiveEvent(
      create(EventSchema, {
        eventId: 'ev-legacy',
        runId: 'run-1',
        sequence: 0n,
        emittedAt: ts(22),
        payload: { case: 'driftDetected', value: d },
      }),
      store,
      0,
    );

    const drifts = store.drifts.list();
    expect(drifts[0].authoredBy ?? '').toBe('');

    const spans: Span[] = [];
    store.spans.queryAgent(GOLDFIVE_ACTOR_ID, 0, 1_000_000, spans);
    const driftSpan = spans.find((s) => s.name === 'looping_reasoning');
    expect(driftSpan?.attributes['drift.authored_by']).toBeUndefined();

    const detail = resolveDriftDetail(drifts[0], [], store);
    expect(detail.authoredBy ?? '').toBe('');
  });
});
