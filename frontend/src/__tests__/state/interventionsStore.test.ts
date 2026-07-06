import { describe, it, expect, vi, beforeEach } from 'vitest';
import { interventionRowFromPb } from '../../lib/interventions';

// A minimal proto-shaped Intervention. The converter only reads fields,
// so a plain object cast to the message type exercises it faithfully.
function pbIntervention(
  over: Record<string, unknown> = {},
): Parameters<typeof interventionRowFromPb>[0] {
  return {
    at: { seconds: 1000n, nanos: 0 },
    source: 'drift',
    kind: 'LOOPING_REASONING',
    bodyOrReason: 'loop',
    author: '',
    outcome: 'plan_revised:r2',
    planRevisionIndex: 2,
    severity: 'warning',
    annotationId: '',
    driftKind: 'looping_reasoning',
    conditionId: '',
    count: 1,
    lifecycle: '',
    firstSeen: undefined,
    lastSeen: undefined,
    severityTransitions: [],
    key: 'drift:d1',
    targetAgentId: 'a:worker',
    driftId: 'd1',
    attemptId: '',
    failureKind: '',
    transitionToStatus: '',
    transitionSource: '',
    transitionTaskId: '',
    targetPlanId: 'p1',
    observations: [],
    ...over,
  } as unknown as Parameters<typeof interventionRowFromPb>[0];
}

describe('interventionRowFromPb', () => {
  it('rebases the absolute timestamp onto the session-relative axis', () => {
    // 1000s absolute, session started at 990s → 10_000ms relative.
    const row = interventionRowFromPb(pbIntervention(), 990_000);
    expect(row.atMs).toBe(10_000);
    expect(row.key).toBe('drift:d1');
    expect(row.source).toBe('drift');
    expect(row.outcome).toBe('plan_revised:r2');
    expect(row.planRevisionIndex).toBe(2);
    expect(row.targetAgentId).toBe('a:worker');
    expect(row.targetPlanId).toBe('p1');
  });

  it('maps an unknown source to goldfive so the view never crashes', () => {
    const row = interventionRowFromPb(pbIntervention({ source: 'martian' }), 0);
    expect(row.source).toBe('goldfive');
  });

  it('leaves observationCount undefined for a single-emit row', () => {
    const row = interventionRowFromPb(pbIntervention({ count: 1 }), 0);
    expect(row.observationCount).toBeUndefined();
  });

  it('surfaces observations + severity transitions on a collapsed row', () => {
    const row = interventionRowFromPb(
      pbIntervention({
        count: 2,
        conditionId: 'cond-1',
        lifecycle: 'escalating',
        severityTransitions: [
          { from: 'warning', to: 'critical', at: { seconds: 1001n, nanos: 0 } },
        ],
        observations: [
          {
            at: { seconds: 1000n, nanos: 0 },
            severity: 'warning',
            lifecycle: 'opened',
            detail: 'first',
            driftId: 'd1',
            prevSeverity: '',
          },
          {
            at: { seconds: 1001n, nanos: 0 },
            severity: 'critical',
            lifecycle: 'escalating',
            detail: 'second',
            driftId: 'd1',
            prevSeverity: 'warning',
          },
        ],
      }),
      1000_000,
    );
    expect(row.observationCount).toBe(2);
    expect(row.currentLifecycle).toBe('escalating');
    expect(row.conditionId).toBe('cond-1');
    expect(row.severityTransitions).toHaveLength(1);
    expect(row.severityTransitions?.[0]).toMatchObject({
      fromSeverity: 'warning',
      toSeverity: 'critical',
    });
    expect(row.observations).toHaveLength(2);
    expect(row.observations?.[1]).toMatchObject({
      seq: 1,
      severity: 'critical',
      prevSeverity: 'warning',
      detail: 'second',
    });
  });

  it('maps transition fields through', () => {
    const row = interventionRowFromPb(
      pbIntervention({
        source: 'transition',
        kind: 'TASK_COMPLETED',
        transitionToStatus: 'COMPLETED',
        transitionSource: 'llm_report',
        transitionTaskId: 't1',
      }),
      0,
    );
    expect(row.source).toBe('transition');
    expect(row.transitionToStatus).toBe('COMPLETED');
    expect(row.transitionSource).toBe('llm_report');
    expect(row.transitionTaskId).toBe('t1');
  });
});

describe('useInterventionsStore.refresh coalescing', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('coalesces concurrent refreshes into at most two client calls', async () => {
    let resolveFirst: (() => void) | undefined;
    const calls: string[] = [];
    const listInterventions = vi.fn((req: { sessionId: string }) => {
      calls.push(req.sessionId);
      if (calls.length === 1) {
        return new Promise((res) => {
          resolveFirst = () => res({ interventions: [] });
        });
      }
      return Promise.resolve({ interventions: [] });
    });
    vi.doMock('../../rpc/client', () => ({
      getHarmonografClient: () => ({ listInterventions }),
    }));
    const { useInterventionsStore } = await import('../../state/interventionsStore');
    const { refresh } = useInterventionsStore.getState();

    // Three concurrent requests while the first is in flight.
    const p1 = refresh('s1', 0);
    const p2 = refresh('s1', 0);
    const p3 = refresh('s1', 0);
    // Only the first call has fired; the others are pending-collapsed.
    expect(calls).toHaveLength(1);
    resolveFirst?.();
    await Promise.all([p1, p2, p3]);
    // Exactly one follow-up ran for the collapsed pending requests.
    expect(calls).toHaveLength(2);
  });
});
