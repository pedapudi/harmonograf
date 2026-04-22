// Pure-function coverage for the client-side intervention deriver.
// Mirrors the server-side tests in server/tests/test_interventions.py so
// the merge + attribution logic stays behavior-identical across the wire.

import { describe, expect, it } from 'vitest';
import {
  deriveInterventions,
  markerRadiusFor,
  SOURCE_COLOR,
} from '../../lib/interventions';
import type { Annotation } from '../../state/annotationStore';
import type { DriftRecord } from '../../gantt/index';
import type { TaskPlan } from '../../gantt/types';

function mkAnnotation(over: Partial<Annotation> = {}): Annotation {
  return {
    id: 'ann_1',
    sessionId: 's',
    spanId: null,
    agentId: 'a',
    atMs: 100,
    author: 'alice',
    kind: 'STEERING',
    body: 'try X',
    createdAtMs: 100,
    deliveredAtMs: null,
    pending: false,
    error: null,
    ...over,
  };
}

function mkDrift(over: Partial<DriftRecord> = {}): DriftRecord {
  return {
    seq: 0,
    kind: 'looping_reasoning',
    severity: 'warning',
    detail: 'loop',
    taskId: 't1',
    agentId: 'a',
    recordedAtMs: 200,
    ...over,
  };
}

function mkPlan(over: Partial<TaskPlan> = {}): TaskPlan {
  return {
    id: 'p1',
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks: [],
    edges: [],
    revisionReason: '',
    revisionKind: '',
    revisionSeverity: '',
    revisionIndex: 0,
    ...over,
  };
}

describe('deriveInterventions', () => {
  it('merges annotations, drifts, and goldfive revisions chronologically', () => {
    const rows = deriveInterventions({
      annotations: [mkAnnotation({ createdAtMs: 110 })],
      drifts: [
        mkDrift({
          seq: 1,
          kind: 'looping_reasoning',
          severity: 'warning',
          recordedAtMs: 120,
        }),
      ],
      plans: [
        // revision inside the 5s window → claimed as outcome of the drift.
        mkPlan({
          id: 'p1',
          createdAtMs: 122,
          revisionKind: 'looping_reasoning',
          revisionIndex: 2,
        }),
        // autonomous cascade_cancel with no preceding drift.
        mkPlan({
          id: 'p2',
          createdAtMs: 200,
          revisionKind: 'cascade_cancel',
          revisionIndex: 3,
        }),
      ],
    });

    expect(rows.map((r) => r.source)).toEqual(['user', 'drift', 'goldfive']);
    // Drift row absorbs the plan_revised outcome; no duplicate plan row.
    expect(rows[1].kind).toBe('LOOPING_REASONING');
    expect(rows[1].outcome).toBe('plan_revised:r2');
    expect(rows[1].planRevisionIndex).toBe(2);
    expect(rows[2].kind).toBe('CASCADE_CANCEL');
    expect(rows[2].source).toBe('goldfive');
  });

  it('tags user_steer drift kind as source=user', () => {
    const rows = deriveInterventions({
      annotations: [],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'user_steer',
          severity: 'info',
          recordedAtMs: 50,
        }),
      ],
      plans: [],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].source).toBe('user');
    expect(rows[0].kind).toBe('STEER');
    expect(rows[0].driftKind).toBe('user_steer');
  });

  it('preserves annotation_id for deep-linking', () => {
    const rows = deriveInterventions({
      annotations: [mkAnnotation({ id: 'ann_steer_42' })],
      drifts: [],
      plans: [],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].annotationId).toBe('ann_steer_42');
    expect(rows[0].author).toBe('alice');
  });

  it('marks a drift as outcome=recorded when no revision follows', () => {
    const rows = deriveInterventions({
      annotations: [],
      drifts: [mkDrift({ seq: 7, kind: 'confabulation_risk' })],
      plans: [],
    });
    expect(rows[0].outcome).toBe('recorded');
  });

  it('produces cascade_cancel:N_tasks when only cancelled tasks follow', () => {
    const rows = deriveInterventions({
      annotations: [],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'runaway_delegation',
          severity: 'critical',
          recordedAtMs: 500,
        }),
      ],
      plans: [
        // Plan after the drift, no revision_kind, but with cancelled tasks.
        mkPlan({
          id: 'p_after',
          createdAtMs: 520,
          revisionKind: '',
          revisionIndex: 0,
          tasks: [
            {
              id: 't1',
              title: '',
              description: '',
              assigneeAgentId: 'a',
              status: 'CANCELLED',
              predictedStartMs: 0,
              predictedDurationMs: 0,
              boundSpanId: '',
            },
            {
              id: 't2',
              title: '',
              description: '',
              assigneeAgentId: 'a',
              status: 'CANCELLED',
              predictedStartMs: 0,
              predictedDurationMs: 0,
              boundSpanId: '',
            },
          ],
        }),
      ],
    });
    expect(rows[0].outcome).toBe('cascade_cancel:2_tasks');
  });

  it('does not double-count a drift + matching plan revision', () => {
    const rows = deriveInterventions({
      annotations: [],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'agent_refusal',
          severity: 'warning',
          recordedAtMs: 600,
        }),
      ],
      plans: [
        mkPlan({
          id: 'p',
          createdAtMs: 601,
          revisionKind: 'agent_refusal',
          revisionIndex: 2,
        }),
      ],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].source).toBe('drift');
    expect(rows[0].outcome).toBe('plan_revised:r2');
  });

  it('returns an empty list for an empty session', () => {
    const rows = deriveInterventions({ annotations: [], drifts: [], plans: [] });
    expect(rows).toEqual([]);
  });
});

describe('marker sizing / palette', () => {
  it('only drift rows grow with severity', () => {
    const baseUser = markerRadiusFor({
      key: 'u',
      atMs: 0,
      source: 'user',
      kind: 'STEER',
      bodyOrReason: '',
      author: '',
      outcome: '',
      planRevisionIndex: 0,
      severity: 'critical',
      annotationId: 'ann',
      driftKind: '',
    });
    const baseDrift = markerRadiusFor({
      key: 'd',
      atMs: 0,
      source: 'drift',
      kind: 'X',
      bodyOrReason: '',
      author: '',
      outcome: '',
      planRevisionIndex: 0,
      severity: 'critical',
      annotationId: '',
      driftKind: 'x',
    });
    expect(baseDrift).toBeGreaterThan(baseUser);
  });

  it('has distinct colours per source', () => {
    expect(SOURCE_COLOR.user).not.toBe(SOURCE_COLOR.drift);
    expect(SOURCE_COLOR.drift).not.toBe(SOURCE_COLOR.goldfive);
  });
});
