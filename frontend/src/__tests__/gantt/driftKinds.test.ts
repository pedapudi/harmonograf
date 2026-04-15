import { describe, expect, it } from 'vitest';
import {
  DRIFT_KIND_META,
  UNKNOWN_DRIFT_KIND_META,
  getDriftKindMeta,
  parseRevisionReason,
  type DriftCategory,
} from '../../gantt/driftKinds';

describe('DRIFT_KIND_META', () => {
  it('covers every drift kind known to the replan pipeline', () => {
    // Spot-check the kinds the iter13/iter15 replan-agent emits so that
    // adding a new drift kind on the backend forces an update here.
    const required = [
      'tool_error',
      'tool_returned_error',
      'tool_unexpected_result',
      'task_failed',
      'task_blocked',
      'task_empty_result',
      'new_work_discovered',
      'task_result_new_work',
      'task_result_contradicts_plan',
      'plan_divergence',
      'agent_reported_divergence',
      'llm_refused',
      'llm_merged_tasks',
      'llm_split_task',
      'llm_reordered_work',
      'context_pressure',
      'user_steer',
      'user_cancel',
      'unexpected_transfer',
      'agent_escalated',
      'multiple_stamp_mismatches',
      'tool_call_wrong_agent',
      'transfer_to_unplanned_agent',
      'failed_span',
      'task_completion_out_of_order',
      'external_signal',
    ];
    for (const k of required) {
      expect(DRIFT_KIND_META[k], `missing meta for ${k}`).toBeDefined();
    }
  });

  it('every entry has icon, color, label, category', () => {
    const validCategories: DriftCategory[] = [
      'error',
      'divergence',
      'discovery',
      'user',
      'structural',
    ];
    for (const [kind, meta] of Object.entries(DRIFT_KIND_META)) {
      expect(meta.icon, `${kind}.icon`).toBeTruthy();
      expect(meta.color, `${kind}.color`).toMatch(/^#[0-9a-fA-F]{6}$/);
      expect(meta.label, `${kind}.label`).toBeTruthy();
      expect(validCategories).toContain(meta.category);
    }
  });
});

describe('parseRevisionReason', () => {
  it('extracts a known kind + detail when input is "kind: detail"', () => {
    const r = parseRevisionReason('tool_error: search_web raised TimeoutError');
    expect(r.kind).toBe('tool_error');
    expect(r.detail).toBe('search_web raised TimeoutError');
    expect(r.meta).toBe(DRIFT_KIND_META['tool_error']);
  });

  it('falls back to UNKNOWN for an unrecognised prefix', () => {
    const r = parseRevisionReason('mystery_drift: surprise');
    expect(r.kind).toBeNull();
    expect(r.detail).toBe('mystery_drift: surprise');
    expect(r.meta).toBe(UNKNOWN_DRIFT_KIND_META);
  });

  it('falls back to UNKNOWN when there is no separator at all', () => {
    const r = parseRevisionReason('plain text revision reason');
    expect(r.kind).toBeNull();
    expect(r.detail).toBe('plain text revision reason');
    expect(r.meta).toBe(UNKNOWN_DRIFT_KIND_META);
  });

  it('handles the empty string defensively', () => {
    const r = parseRevisionReason('');
    expect(r.kind).toBeNull();
    expect(r.detail).toBe('');
    expect(r.meta).toBe(UNKNOWN_DRIFT_KIND_META);
  });

  it('does not split on a bare colon (only "kind: ")', () => {
    // A timestamp like "12:34:56" embedded mid-message must not be parsed
    // as a drift kind.
    const r = parseRevisionReason('12:34:56 something happened');
    expect(r.kind).toBeNull();
  });

  it('preserves the full reason for the tooltip via meta lookup', () => {
    const r = parseRevisionReason('llm_split_task: ResearchAgent split into A,B');
    expect(r.meta.label).toBe('Split task');
    expect(r.meta.category).toBe('structural');
  });

  it('routes new_work_discovered into the discovery category', () => {
    const r = parseRevisionReason('new_work_discovered: needs followup');
    expect(r.meta.category).toBe('discovery');
  });

  it('routes user_steer into the user category', () => {
    const r = parseRevisionReason('user_steer: focus on accessibility');
    expect(r.meta.category).toBe('user');
  });
});

describe('getDriftKindMeta', () => {
  it('returns the meta for a known kind', () => {
    expect(getDriftKindMeta('task_failed')).toBe(DRIFT_KIND_META['task_failed']);
  });
  it('returns UNKNOWN for unknown kinds and falsy values', () => {
    expect(getDriftKindMeta('nope')).toBe(UNKNOWN_DRIFT_KIND_META);
    expect(getDriftKindMeta(null)).toBe(UNKNOWN_DRIFT_KIND_META);
    expect(getDriftKindMeta(undefined)).toBe(UNKNOWN_DRIFT_KIND_META);
    expect(getDriftKindMeta('')).toBe(UNKNOWN_DRIFT_KIND_META);
  });
});
