// resolveTaskTransitionDetail — goldfive#267 / #251 R4.
//
// Verifies the InterventionDetail composition for TaskTransitioned
// selections. The resolver maps the transition record onto the same
// three-section shape as resolveDriftDetail so the existing detail
// panel can render transitions without a new component.

import { describe, expect, it } from 'vitest';
import type { TaskTransitionRecord } from '../../gantt/index';
import { resolveTaskTransitionDetail } from '../../lib/interventionDetail';

function mkRecord(over: Partial<TaskTransitionRecord> = {}): TaskTransitionRecord {
  return {
    seq: 0,
    runId: 'run-1',
    taskId: 't-7',
    fromStatus: 'RUNNING',
    toStatus: 'COMPLETED',
    source: 'llm_report',
    revisionStamp: 0,
    agentName: 'presentation-orchestrated-abc:researcher_agent',
    invocationId: 'inv-7',
    recordedAtMs: 100,
    recordedAtAbsoluteMs: 100,
    ...over,
  };
}

describe('resolveTaskTransitionDetail', () => {
  it('composes the steering section as "FROM → TO via SOURCE"', () => {
    const detail = resolveTaskTransitionDetail(mkRecord());
    expect(detail.steering).toContain('RUNNING → COMPLETED');
    expect(detail.steering).toContain('source: llm_report');
    expect(detail.targetAgentId).toBe(
      'presentation-orchestrated-abc:researcher_agent',
    );
    expect(detail.targetTaskId).toBe('t-7');
    expect(detail.trigger).toBe('');
  });

  it('omits the plan-rev line when revisionStamp is zero', () => {
    const detail = resolveTaskTransitionDetail(mkRecord({ revisionStamp: 0 }));
    expect(detail.steering).not.toContain('plan rev:');
  });

  it('includes the plan-rev line when revisionStamp is non-zero', () => {
    const detail = resolveTaskTransitionDetail(mkRecord({ revisionStamp: 4 }));
    expect(detail.steering).toContain('plan rev: 4');
  });

  it('includes the invocation id when present', () => {
    const detail = resolveTaskTransitionDetail(mkRecord({ invocationId: 'inv-99' }));
    expect(detail.steering).toContain('invocation: inv-99');
  });

  it('omits the invocation line when invocationId is empty', () => {
    const detail = resolveTaskTransitionDetail(mkRecord({ invocationId: '' }));
    expect(detail.steering).not.toContain('invocation:');
  });

  it('handles missing fromStatus gracefully', () => {
    const detail = resolveTaskTransitionDetail(
      mkRecord({ fromStatus: '', toStatus: 'CANCELLED' }),
    );
    expect(detail.steering).toContain('CANCELLED');
    expect(detail.steering).not.toContain('→');
  });
});
