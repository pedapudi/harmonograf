// useSessionWatch → loadPlanHistory backfill integration.
//
// Regression: historical / COMPLETED sessions only surfaced the latest
// plan revision (the Trajectory view's REVISIONS strip showed only "REV
// N" with earlier revs + their DAG contents missing). Root cause was
// that the live event stream replay for a completed session doesn't
// fan out individual plan_submitted / plan_revised events — the server
// sends a collapsed latest snapshot — so the PlanHistoryRegistry never
// sees the earlier revs unless we pull them from the unary
// GetSessionPlanHistory RPC on session open.
//
// This test mounts useSessionWatch with a mocked Connect client whose
// `getSessionPlanHistory` returns three revisions and whose
// `watchSession` emits a single `session` frame. After the frame
// lands, we assert every revision made it into the registry and that
// a RevisionScrubber rendered against the same history exposes one
// notch per revision (the user-visible "REV N" strip).

import { create } from '@bufbuild/protobuf';
import { render, renderHook, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import { RevisionScrubber } from '../../components/shell/views/RevisionScrubber';
import {
  PlanSchema,
  TaskSchema,
  DriftKind,
  DriftSeverity,
} from '../../pb/goldfive/v1/types_pb';

// getHarmonografClient is called both inside useSessionWatch (for
// watchSession) and inside loadPlanHistory (for getSessionPlanHistory).
// We mock the module so both reach into the same stub.
const mockClient = {
  watchSession: vi.fn(),
  getSessionPlanHistory: vi.fn(),
};
vi.mock('../../rpc/client', () => ({
  getHarmonografClient: () => mockClient,
}));

// Import after the mock is registered so the hook picks up the stub.
import { useSessionWatch, getSessionStore } from '../../rpc/hooks';

function pbTask(id: string) {
  return create(TaskSchema, {
    id,
    title: `Task ${id}`,
    description: '',
    assigneeAgentId: 'agent-a',
    status: 0,
    predictedStartMs: 0n,
    predictedDurationMs: 0n,
  });
}

function pbPlan(id: string, taskIds: string[], revisionIndex: number) {
  return create(PlanSchema, {
    id,
    runId: 'run-1',
    summary: `plan rev ${revisionIndex}`,
    tasks: taskIds.map(pbTask),
    edges: [],
    revisionReason: revisionIndex === 0 ? '' : `rev ${revisionIndex} reason`,
    revisionKind:
      revisionIndex === 0 ? DriftKind.UNSPECIFIED : DriftKind.OFF_TOPIC,
    revisionSeverity:
      revisionIndex === 0 ? DriftSeverity.UNSPECIFIED : DriftSeverity.WARNING,
    revisionIndex,
    revisionTriggerEventId: revisionIndex === 0 ? '' : `drift-${revisionIndex}`,
  });
}

// Async iterable that yields one `session` frame then closes. Matches
// the minimal shape useSessionWatch iterates over: { kind: { case,
// value } }. No other frames are needed to observe the backfill.
function makeSessionStream(sessionCreatedAtMs: number) {
  return {
    async *[Symbol.asyncIterator]() {
      yield {
        kind: {
          case: 'session',
          value: {
            status: 2, // COMPLETED
            createdAt: {
              seconds: BigInt(Math.floor(sessionCreatedAtMs / 1000)),
              nanos: 0,
            },
          },
        },
      };
    },
  };
}

beforeEach(() => {
  mockClient.watchSession.mockReset();
  mockClient.getSessionPlanHistory.mockReset();
});

describe('useSessionWatch plan-history backfill', () => {
  it('seeds PlanHistoryRegistry with every revision for a completed session', async () => {
    const sessionId = `sess-backfill-${Math.random().toString(36).slice(2)}`;

    mockClient.watchSession.mockImplementation(() =>
      makeSessionStream(1_700_000_000_000),
    );
    mockClient.getSessionPlanHistory.mockImplementation(
      async (req: { sessionId: string }) => {
        expect(req.sessionId).toBe(sessionId);
        return {
          revisions: [
            {
              plan: pbPlan('p1', ['t1'], 0),
              revisionNumber: 0,
              revisionReason: '',
              revisionKind: 'UNSPECIFIED',
              revisionTriggerEventId: '',
              emittedAt: { seconds: 1_700_000_000n, nanos: 0 },
            },
            {
              plan: pbPlan('p1', ['t1', 't2'], 1),
              revisionNumber: 1,
              revisionReason: 'off topic',
              revisionKind: 'OFF_TOPIC',
              revisionTriggerEventId: 'drift-1',
              emittedAt: { seconds: 1_700_000_010n, nanos: 0 },
            },
            {
              plan: pbPlan('p1', ['t1', 't2', 't3'], 2),
              revisionNumber: 2,
              revisionReason: 'user expand',
              revisionKind: 'USER_STEER',
              revisionTriggerEventId: 'ann-2',
              emittedAt: { seconds: 1_700_000_020n, nanos: 0 },
            },
          ],
        };
      },
    );

    renderHook(() => useSessionWatch(sessionId));

    // Backfill fires in the session-frame handler inside the async
    // watchSession iterator. Wait for both the unary call and the
    // registry population.
    await waitFor(() => {
      expect(mockClient.getSessionPlanHistory).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      const store = getSessionStore(sessionId);
      expect(store).toBeDefined();
      expect(store!.planHistory.historyFor('p1')).toHaveLength(3);
    });

    const store = getSessionStore(sessionId)!;
    const history = store.planHistory.historyFor('p1');
    expect(history.map((r) => r.revision)).toEqual([0, 1, 2]);
    expect(history[1].kind).toBe('off_topic');
    expect(history[2].kind).toBe('user_steer');

    // User-visible assertion: the scrubber renders one notch per
    // revision (plus the "Latest" sentinel) — before the fix, only the
    // last revision made it here and the strip collapsed to a single
    // "REV N" entry.
    render(
      <RevisionScrubber
        history={history}
        pinnedRevision={null}
        onPinRevision={() => {}}
      />,
    );
    expect(screen.getByTestId('scrubber-notch-0')).toBeInTheDocument();
    expect(screen.getByTestId('scrubber-notch-1')).toBeInTheDocument();
    expect(screen.getByTestId('scrubber-notch-2')).toBeInTheDocument();
  });

  it('swallows RPC errors without crashing the watch stream', async () => {
    const sessionId = `sess-backfill-err-${Math.random().toString(36).slice(2)}`;

    mockClient.watchSession.mockImplementation(() =>
      makeSessionStream(1_700_000_000_000),
    );
    mockClient.getSessionPlanHistory.mockRejectedValue(
      new Error('RPC transport dropped'),
    );

    // Silence the expected warning so the test output stays clean.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});

    renderHook(() => useSessionWatch(sessionId));

    await waitFor(() => {
      expect(mockClient.getSessionPlanHistory).toHaveBeenCalledTimes(1);
    });
    // Registry is empty (no revisions delivered) but the store exists
    // and the hook did not crash.
    const store = getSessionStore(sessionId);
    expect(store).toBeDefined();
    expect(store!.planHistory.historyFor('p1')).toEqual([]);

    warn.mockRestore();
  });
});
