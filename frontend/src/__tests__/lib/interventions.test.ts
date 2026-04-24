// Pure-function coverage for the client-side intervention deriver.
// Mirrors the server-side tests in server/tests/test_interventions.py so
// the merge + attribution logic stays behavior-identical across the wire.

import { describe, expect, it, vi } from 'vitest';
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
    recordedAtAbsoluteMs: 200,
    annotationId: '',
    driftId: '',
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
    triggerEventId: '',
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
          driftId: 'drift_loop_1',
        }),
      ],
      plans: [
        // Revision with strict triggerEventId matching the drift.
        mkPlan({
          id: 'p1',
          createdAtMs: 122,
          revisionKind: 'looping_reasoning',
          revisionIndex: 2,
          triggerEventId: 'drift_loop_1',
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
              supersedes: '',
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
              supersedes: '',
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
          driftId: 'drift_refusal_1',
        }),
      ],
      plans: [
        mkPlan({
          id: 'p',
          createdAtMs: 601,
          revisionKind: 'agent_refusal',
          revisionIndex: 2,
          triggerEventId: 'drift_refusal_1',
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

  // harmonograf#75 — dedup by annotation_id across annotation / drift / plan
  // sources so a single user STEER renders as one card, not three.

  it('collapses annotation + USER_STEER drift + plan revision into one card', () => {
    const rows = deriveInterventions({
      annotations: [
        mkAnnotation({
          id: 'ann_s1',
          createdAtMs: 100,
          author: 'alice',
          body: 'focus on intro',
        }),
      ],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'user_steer',
          severity: 'warning',
          detail: 'by alice: focus on intro',
          recordedAtMs: 120,
          annotationId: 'ann_s1',
        }),
      ],
      plans: [
        mkPlan({
          id: 'p',
          createdAtMs: 180,
          revisionKind: 'user_steer',
          revisionSeverity: 'warning',
          revisionIndex: 1,
          triggerEventId: 'ann_s1',
        }),
      ],
    });

    // Exactly one card, not three.
    expect(rows).toHaveLength(1);
    const card = rows[0];
    expect(card.source).toBe('user');
    expect(card.kind).toBe('STEER');
    expect(card.annotationId).toBe('ann_s1');
    expect(card.author).toBe('alice');
    expect(card.bodyOrReason).toBe('focus on intro');
    // Drift + plan outcomes folded in.
    expect(card.severity).toBe('warning');
    expect(card.outcome).toBe('plan_revised:r1');
    expect(card.planRevisionIndex).toBe(1);
  });

  it('autonomous drift keeps its own card alongside a user annotation', () => {
    const rows = deriveInterventions({
      annotations: [
        mkAnnotation({ id: 'ann_u', createdAtMs: 100, author: 'alice', body: 'pivot' }),
      ],
      drifts: [
        // Autonomous drift — no annotation_id, own card.
        mkDrift({
          seq: 1,
          kind: 'looping_reasoning',
          severity: 'warning',
          recordedAtMs: 500,
          annotationId: '',
        }),
        // User-control drift carrying the annotation id — must merge.
        mkDrift({
          seq: 2,
          kind: 'user_steer',
          severity: 'warning',
          recordedAtMs: 110,
          annotationId: 'ann_u',
        }),
      ],
      plans: [],
    });
    expect(rows).toHaveLength(2);
    const user = rows.find((r) => r.source === 'user');
    const drift = rows.find((r) => r.source === 'drift');
    expect(user?.annotationId).toBe('ann_u');
    expect(drift?.kind).toBe('LOOPING_REASONING');
    expect(drift?.annotationId).toBe('');
  });

  it('user_steer drift without annotation_id keeps a separate card (back-compat)', () => {
    // Pre-goldfive#176 emissions had no annotation_id; the deduper has
    // nothing to join on, so the drift rides through as its own card.
    const rows = deriveInterventions({
      annotations: [mkAnnotation({ id: 'ann_x', createdAtMs: 100 })],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'user_steer',
          severity: 'warning',
          recordedAtMs: 140,
          annotationId: '',
        }),
      ],
      plans: [],
    });
    expect(rows).toHaveLength(2);
  });

  // harmonograf#99 rescope — strict-id dedup removes the time-window
  // fragility entirely. A user STEER with an arbitrary refine delay now
  // collapses to one card because the plan carries the source
  // annotation_id as its triggerEventId.
  it('collapses STEER + drift + slow plan revision (20min gap) via strict id', () => {
    const rows = deriveInterventions({
      annotations: [
        mkAnnotation({
          id: 'ann_slow',
          createdAtMs: 100_200,
          author: 'alice',
          body: 'pivot to solar flares',
        }),
      ],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'user_steer',
          severity: 'warning',
          detail: 'by alice: pivot to solar flares',
          recordedAtMs: 100_300,
          annotationId: 'ann_slow',
          driftId: 'drift_slow_steer',
        }),
      ],
      plans: [
        mkPlan({
          id: 'p_slow',
          // 20 minutes after the drift — strict-id doesn't care.
          createdAtMs: 1_300_500,
          revisionKind: 'user_steer',
          revisionSeverity: 'warning',
          revisionIndex: 1,
          triggerEventId: 'ann_slow',
        }),
      ],
    });

    expect(rows).toHaveLength(1);
    const card = rows[0];
    expect(card.source).toBe('user');
    expect(card.kind).toBe('STEER');
    expect(card.annotationId).toBe('ann_slow');
    expect(card.author).toBe('alice');
    expect(card.bodyOrReason).toBe('pivot to solar flares');
    expect(card.severity).toBe('warning');
    expect(card.outcome).toBe('plan_revised:r1');
    expect(card.planRevisionIndex).toBe(1);
  });

  it('autonomous drift + mismatched plan keeps two cards (harmonograf#99)', () => {
    // Strict-id: a drift with one id + a plan with a different id do
    // not merge. Preserves the intent of the old 5s-window test
    // (unrelated revisions can't claim-steal each other) via exact-id
    // matching rather than a heuristic window.
    const rows = deriveInterventions({
      annotations: [],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'looping_reasoning',
          severity: 'warning',
          recordedAtMs: 100_000,
          driftId: 'drift_loop_A',
        }),
      ],
      plans: [
        mkPlan({
          id: 'p_autonomous',
          createdAtMs: 160_000,
          revisionKind: 'looping_reasoning',
          revisionIndex: 2,
          triggerEventId: 'drift_loop_B_unrelated',
        }),
      ],
    });
    expect(rows).toHaveLength(2);
    const drift = rows.find(
      (r) => r.source === 'drift' && !r.outcome.startsWith('plan_revised:'),
    );
    const plan = rows.find((r) => r.outcome.startsWith('plan_revised:'));
    expect(drift?.outcome).toBe('recorded');
    expect(plan?.planRevisionIndex).toBe(2);
  });

  // harmonograf#101 — legacy time-window opt-in now flows through the
  // ``legacyPlanAttributionWindowMs`` option, not ``import.meta.env``.
  // Coverage mirrors the server tests in test_interventions.py.

  it('legacy window default (0) leaves orphan plan as its own card', () => {
    // Plan row with no triggerEventId cannot strict-merge; with the
    // window disabled (default) the aggregator does NOT fold it onto
    // the preceding user-control rows — two cards survive.
    const rows = deriveInterventions({
      annotations: [
        mkAnnotation({ id: 'ann_lw_off', createdAtMs: 100_000 }),
      ],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'user_steer',
          severity: 'warning',
          recordedAtMs: 100_200,
          annotationId: 'ann_lw_off',
          driftId: 'drift_lw_off',
        }),
      ],
      plans: [
        mkPlan({
          id: 'p_lw_off',
          createdAtMs: 700_000,
          revisionKind: 'user_steer',
          revisionSeverity: 'warning',
          revisionIndex: 1,
          // No triggerEventId.
        }),
      ],
      // legacyPlanAttributionWindowMs omitted → default 0 / disabled.
    });

    expect(rows).toHaveLength(2);
    const merged = rows.find((r) => r.annotationId === 'ann_lw_off');
    const orphan = rows.find(
      (r) => r.planRevisionIndex === 1 && !r.annotationId,
    );
    expect(merged?.outcome).toBe('recorded');
    expect(orphan?.outcome).toBe('plan_revised:r1');
  });

  it('legacy window enabled via option merges + warns', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const rows = deriveInterventions({
        annotations: [
          mkAnnotation({ id: 'ann_lw_on', createdAtMs: 100_000 }),
        ],
        drifts: [
          mkDrift({
            seq: 0,
            kind: 'user_steer',
            severity: 'warning',
            recordedAtMs: 100_200,
            annotationId: 'ann_lw_on',
            driftId: 'drift_lw_on',
          }),
        ],
        plans: [
          mkPlan({
            id: 'p_lw_on',
            createdAtMs: 700_000, // 10min past annotation — inside 15min
            revisionKind: 'user_steer',
            revisionSeverity: 'warning',
            revisionIndex: 1,
            // No triggerEventId; must fall back to time-window.
          }),
        ],
        legacyPlanAttributionWindowMs: 900_000,
      });

      // Annotation + drift collapse via strict id; the orphan plan row
      // folds onto the drift via the legacy fallback, so exactly one
      // user-sourced card with the plan_revised outcome survives.
      const userCards = rows.filter((r) => r.source === 'user');
      expect(userCards).toHaveLength(1);
      expect(userCards[0].outcome).toBe('plan_revised:r1');

      // Warning must reference the new option name, not the old env var.
      expect(warnSpy).toHaveBeenCalled();
      const message = warnSpy.mock.calls
        .map((args) => args.join(' '))
        .join('\n');
      expect(message).toContain('legacyPlanAttributionWindowMs');
      expect(message).not.toContain(
        'VITE_HARMONOGRAF_LEGACY_PLAN_ATTRIBUTION_WINDOW_MS',
      );
    } finally {
      warnSpy.mockRestore();
    }
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
      triggerEventId: 'ann',
      targetAgentId: '',
      driftId: '',
      attemptId: '',
      failureKind: '',
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
      triggerEventId: '',
      targetAgentId: '',
      driftId: '',
      attemptId: '',
      failureKind: '',
    });
    expect(baseDrift).toBeGreaterThan(baseUser);
  });

  it('has distinct colours per source', () => {
    expect(SOURCE_COLOR.user).not.toBe(SOURCE_COLOR.drift);
    expect(SOURCE_COLOR.drift).not.toBe(SOURCE_COLOR.goldfive);
  });
});
