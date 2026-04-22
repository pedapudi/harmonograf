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
    annotationId: '',
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

  // harmonograf#86 — user STEERs involve a planner LLM round-trip that
  // can take 30-90s on large local models. The pre-#86 deriver used a
  // flat 5s attribution window and stranded the drift row + emitted a
  // separate plan-sourced card whose atMs was the plan's createdAtMs
  // (rendered as 6:33), alongside the annotation row rendered with an
  // absolute-ms timestamp bug (29613779:31). Both symptoms go away when
  // the user-control kinds use the extended window AND the annotation's
  // createdAtMs is session-relative (fixed in convertAnnotation).
  it('collapses STEER + drift + slow plan revision (70s gap) into one card', () => {
    const rows = deriveInterventions({
      annotations: [
        mkAnnotation({
          id: 'ann_slow',
          createdAtMs: 100_200, // session-relative — the #86 convert.ts fix
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
        }),
      ],
      plans: [
        mkPlan({
          id: 'p_slow',
          // 70s after the drift — way outside the 5s default window, but
          // inside the user-control 5-minute extended window.
          createdAtMs: 170_300,
          revisionKind: 'user_steer',
          revisionSeverity: 'warning',
          revisionIndex: 1,
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

  it('collapses very-slow STEER refine via plan.revisionAnnotationId (harmonograf#95)', () => {
    // kikuchi/Qwen3.5-35B case: refine lands 20 minutes after the drift —
    // OUTSIDE the widened 15-min time window. The strict-id stamp on the
    // plan (goldfive#196) is what actually merges this across the gap.
    const twentyMinutesMs = 20 * 60 * 1000;
    const rows = deriveInterventions({
      annotations: [
        mkAnnotation({
          id: 'ann_very_slow',
          createdAtMs: 100_000,
          author: 'alice',
          body: 'pivot',
        }),
      ],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'user_steer',
          severity: 'warning',
          detail: 'by alice: pivot',
          recordedAtMs: 100_200,
          annotationId: 'ann_very_slow',
        }),
      ],
      plans: [
        mkPlan({
          id: 'p_very_slow',
          createdAtMs: 100_000 + twentyMinutesMs,
          revisionKind: 'user_steer',
          revisionSeverity: 'warning',
          revisionIndex: 1,
          // goldfive#196 plan stamp — the only thing that can collapse
          // this gap now that the time window is 15 min (still < 20 min).
          revisionAnnotationId: 'ann_very_slow',
        }),
      ],
    });
    expect(rows).toHaveLength(1);
    const card = rows[0];
    expect(card.annotationId).toBe('ann_very_slow');
    expect(card.outcome).toBe('plan_revised:r1');
    expect(card.planRevisionIndex).toBe(1);
  });

  it('falls back to widened 15-min window for pre-#196 data (no plan stamp)', () => {
    // Pre-goldfive#196 producer: plan row has no revisionAnnotationId.
    // The drift has one; the 14-min gap is inside the widened 900s/15-min
    // fallback window, so _find_merge_target still folds it in.
    const fourteenMinutesMs = 14 * 60 * 1000;
    const rows = deriveInterventions({
      annotations: [
        mkAnnotation({
          id: 'ann_fb',
          createdAtMs: 100_000,
          author: 'alice',
          body: 'pivot',
        }),
      ],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'user_steer',
          severity: 'warning',
          detail: 'by alice: pivot',
          recordedAtMs: 100_200,
          annotationId: 'ann_fb',
        }),
      ],
      plans: [
        mkPlan({
          id: 'p_fb',
          createdAtMs: 100_000 + fourteenMinutesMs,
          revisionKind: 'user_steer',
          revisionSeverity: 'warning',
          revisionIndex: 1,
          revisionAnnotationId: '',  // pre-#196
        }),
      ],
    });
    expect(rows).toHaveLength(1);
    const card = rows[0];
    expect(card.annotationId).toBe('ann_fb');
    expect(card.outcome).toBe('plan_revised:r1');
  });

  it('autonomous slow drift still fires two cards (tight 5s window unchanged)', () => {
    // Autonomous kinds keep the default 5s window so a slow refine here
    // doesn't claim-steal an unrelated later revision. Ensures the
    // widened user-control window does NOT bleed across kinds.
    const rows = deriveInterventions({
      annotations: [],
      drifts: [
        mkDrift({
          seq: 0,
          kind: 'looping_reasoning',
          severity: 'warning',
          recordedAtMs: 100_000,
        }),
      ],
      plans: [
        mkPlan({
          id: 'p_autonomous',
          createdAtMs: 160_000, // 60s gap — autonomous window is 5s
          revisionKind: 'looping_reasoning',
          revisionIndex: 2,
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
