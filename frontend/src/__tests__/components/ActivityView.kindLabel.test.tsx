/**
 * Item 1 of UX cleanup batch — Activity view drift-row labelling.
 *
 * Drift-detected events (off_topic, plan_divergence, refine_steer, …)
 * synthesise CUSTOM-kind spans on the goldfive lane. The Activity view
 * used to render these rows with the bare "CUSTOM" enum value, which
 * conveyed nothing about what the row actually represents. The
 * ``activityKindLabel`` helper now inspects well-known goldfive / drift
 * attributes and surfaces a useful label instead.
 */

import { describe, it, expect } from 'vitest';
import { activityKindLabel } from '../../components/shell/views/activityKind';
import type { Span } from '../../gantt/types';

function mkSpan(overrides: Partial<Span> = {}): Span {
  return {
    id: 'span-1',
    sessionId: 'sess',
    agentId: 'agent-1',
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name: '',
    startMs: 0,
    endMs: 0,
    links: [],
    attributes: {},
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
    ...overrides,
  };
}

describe('Activity view kind label', () => {
  it('renders SpanKind enum value for non-CUSTOM kinds unchanged', () => {
    expect(activityKindLabel(mkSpan({ kind: 'INVOCATION' }))).toBe('INVOCATION');
    expect(activityKindLabel(mkSpan({ kind: 'LLM_CALL' }))).toBe('LLM_CALL');
    expect(activityKindLabel(mkSpan({ kind: 'TOOL_CALL' }))).toBe('TOOL_CALL');
    expect(activityKindLabel(mkSpan({ kind: 'USER_MESSAGE' }))).toBe('USER_MESSAGE');
  });

  it('surfaces the drift kind on synthesised drift spans', () => {
    const offTopic = mkSpan({
      attributes: {
        'drift.kind': { kind: 'string', value: 'off_topic' },
      },
    });
    expect(activityKindLabel(offTopic)).toBe('OFF_TOPIC');

    const planDivergence = mkSpan({
      attributes: {
        'drift.kind': { kind: 'string', value: 'plan_divergence' },
      },
    });
    expect(activityKindLabel(planDivergence)).toBe('PLAN_DIVERGENCE');

    const userSteer = mkSpan({
      attributes: {
        'drift.kind': { kind: 'string', value: 'user_steer' },
      },
    });
    expect(activityKindLabel(userSteer)).toBe('USER_STEER');
  });

  it('surfaces refine spans with the triggering drift kind', () => {
    // Synthesised refine spans carry ``refine.kind`` (the drift kind
    // that drove the refine).
    const refine = mkSpan({
      attributes: {
        'refine.kind': { kind: 'string', value: 'plan_divergence' },
      },
    });
    expect(activityKindLabel(refine)).toBe('REFINE: PLAN_DIVERGENCE');
  });

  it('surfaces translated goldfive call_name (refine_steer, judge_*)', () => {
    const refineSteer = mkSpan({
      attributes: {
        'goldfive.call_name': { kind: 'string', value: 'refine_steer' },
      },
    });
    expect(activityKindLabel(refineSteer)).toBe('REFINE_STEER');

    const judge = mkSpan({
      attributes: {
        'goldfive.call_name': { kind: 'string', value: 'judge_reasoning' },
      },
    });
    expect(activityKindLabel(judge)).toBe('JUDGE_REASONING');
  });

  it('falls back to the span name when no goldfive/drift attribute is present', () => {
    const named = mkSpan({ name: 'cooperative_cancel' });
    expect(activityKindLabel(named)).toBe('COOPERATIVE_CANCEL');
  });

  it('falls back to bare "CUSTOM" only when no useful attribute or name is set', () => {
    const bare = mkSpan({ name: '' });
    expect(activityKindLabel(bare)).toBe('CUSTOM');
  });

  it('covers every DriftKind enum value via the drift.kind attribute path', () => {
    // The well-known DriftKind enum values that appear on drift spans
    // emitted by goldfive. Each surfaces as the uppercase form. This
    // protects against the regression of one-off cases sneaking back
    // into the activity view via a stale "CUSTOM" string.
    const driftKinds = [
      'tool_error',
      'agent_refusal',
      'new_work_discovered',
      'plan_divergence',
      'user_steer',
      'user_cancel',
      'task_failed_recoverable',
      'task_failed_fatal',
      'context_pressure',
      'blocked',
      'wrong_agent',
      'agent_transfer',
      'model_refusal',
      'stopped_early',
      'too_many_steps',
      'goal_unreachable',
      'task_timeout',
      'repeated_failure',
      'unexpected_output',
      'schema_violation',
      'hallucination_suspected',
      'safety_concern',
      'resource_exhausted',
      'ambiguous_intent',
      'looping_tool_call',
      'looping_reasoning',
      'confusion',
      'off_topic',
      'intent_divergence',
      'uncertain_progress',
      'self_reported_stuck',
      'reasoning_cluster_tightening',
      'confabulation_risk',
      'runaway_delegation',
      'refine_validation_failed',
      'human_intervention_required',
      'goal_drift',
    ];
    for (const k of driftKinds) {
      const s = mkSpan({
        attributes: { 'drift.kind': { kind: 'string', value: k } },
      });
      expect(activityKindLabel(s)).toBe(k.toUpperCase());
    }
  });
});
