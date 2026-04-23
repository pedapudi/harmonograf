// Regression test for the STATUS_QUERY polling thrash bug in
// components/shell/views/GraphView.tsx.
//
// The offending useEffect was intended to fire sendStatusQuery once on
// mount and then every 8s while there are running agents. In practice,
// its dependency array included `layout.agentIds` and
// `layout.activations` — both derived from a memo keyed on `tick`, so
// both got fresh array identities every store tick (~60Hz during a
// busy run). That caused the effect to re-run on every render,
// firing the initial `poll()` again each time → ~222 sendStatusQuery
// calls/sec observed in production, 50k+ DB rows in under 4 minutes.
//
// This test mounts <GraphView /> with one running agent, fires 20
// store-subscription ticks in quick succession, and asserts
// sendStatusQuery was called at most once (the initial poll) — NOT
// 20+ times. It also verifies that when the set of running agents
// actually changes, we DO fire an extra immediate poll.

import { act, render } from '@testing-library/react';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Agent, Span } from '../../gantt/types';

type WatchMock = {
  store: SessionStore;
  connected: boolean;
  initialBurstComplete: boolean;
  error: string | null;
  sessionStatus: 'UNKNOWN' | 'LIVE' | 'COMPLETED' | 'ABORTED';
  lastEventAtMs: number;
};

let mockStore: SessionStore | undefined;
let mockWatch: WatchMock | undefined;
let mockSessionId: string | null = 'session-1';
const sendStatusQueryMock = vi.fn().mockResolvedValue('');

vi.mock('../../rpc/hooks', async () => {
  const actual =
    await vi.importActual<typeof import('../../rpc/hooks')>(
      '../../rpc/hooks',
    );
  return {
    ...actual,
    getSessionStore: (id: string | null) => (id ? mockStore : undefined),
    useSessionWatch: () => mockWatch ?? null,
    sendStatusQuery: (...args: unknown[]) => sendStatusQueryMock(...args),
  };
});

vi.mock('../../state/uiStore', () => {
  const state: Record<string, unknown> = {
    currentSessionId: 'session-1',
    selectSpan: () => {},
    selectTask: () => {},
    selectedSpanId: null,
    selectedTaskId: null,
    taskPlanMode: 'ghost' as const,
    taskPlanVisible: false,
    setTaskPlanMode: () => {},
    toggleTaskPlanVisible: () => {},
    graphViewport: null,
    setGraphViewport: () => {},
    setGraphActions: () => {},
  };
  return {
    useUiStore: <T,>(selector: (s: typeof state) => T) => {
      return selector({ ...state, currentSessionId: mockSessionId ?? '' });
    },
  };
});

import { GraphView } from '../../components/shell/views/GraphView';

function agent(id: string, connectedAtMs = 0): Agent {
  return {
    id,
    name: id,
    framework: 'ADK',
    capabilities: [],
    status: 'CONNECTED',
    connectedAtMs,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
  };
}

function invocationSpan(
  id: string,
  agentId: string,
  startMs: number,
  endMs: number | null,
): Span {
  return {
    id,
    sessionId: 'session-1',
    agentId,
    parentSpanId: null,
    kind: 'INVOCATION',
    name: `${agentId}-inv`,
    status: endMs === null ? 'RUNNING' : 'COMPLETED',
    startMs,
    endMs,
    lane: 0,
    attributes: {},
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
  };
}

describe('<GraphView /> STATUS_QUERY polling stability', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    sendStatusQueryMock.mockClear();
    mockStore = new SessionStore();
    mockSessionId = 'session-1';
    mockStore.agents.upsert(agent('coordinator', 1));
    mockStore.agents.upsert(agent('worker', 2));
    // One open (running) INVOCATION on 'worker'.
    mockStore.spans.append(invocationSpan('sp-worker-1', 'worker', 1_000, null));
    mockWatch = {
      store: mockStore,
      connected: true,
      initialBurstComplete: true,
      error: null,
      sessionStatus: 'LIVE',
      lastEventAtMs: 0,
    };
  });

  afterEach(() => {
    vi.useRealTimers();
    mockStore = undefined;
    mockWatch = undefined;
  });

  it('does not re-poll on every store-subscription tick when the running set is unchanged', () => {
    render(<GraphView />);
    // Initial mount: one immediate poll for the single running agent.
    const initialCalls = sendStatusQueryMock.mock.calls.length;
    expect(initialCalls).toBe(1);
    expect(sendStatusQueryMock.mock.calls[0][1]).toBe('worker');

    // Simulate 20 store ticks that do NOT change the running agent set
    // (just bumping activity/status — exactly the kind of churn
    // delegation events, context series appends, or per-frame nowMs
    // updates produce in a live session). Each emit happens in its
    // own act() so React commits a render between them, which is what
    // a real live session produces (subscribe callbacks fire one at a
    // time across microtasks).
    for (let i = 0; i < 20; i++) {
      act(() => {
        mockStore!.agents.setActivityAndStuck(
          'coordinator',
          `beat-${i}`,
          false,
        );
      });
    }

    // The effect's deps must be stable enough that it does NOT re-run
    // on every tick. Without the fix, we'd see ~21 calls (1 initial +
    // 20 re-mounts firing poll() again). With the fix, we see exactly
    // the initial call.
    expect(sendStatusQueryMock.mock.calls.length).toBe(initialCalls);
  });

  it('fires an extra immediate poll when the running-agent set actually changes', () => {
    render(<GraphView />);
    expect(sendStatusQueryMock.mock.calls.length).toBe(1);

    // A second agent goes running — set changed, effect re-runs, new
    // immediate poll fires for BOTH currently-running agents.
    act(() => {
      mockStore!.spans.append(
        invocationSpan('sp-coord-1', 'coordinator', 2_000, null),
      );
    });

    // After the set-change, one more poll cycle fires. That cycle
    // iterates the current running set (both agents now), so we
    // expect 1 (initial) + 2 (second cycle, one per running agent)
    // = 3 total.
    expect(sendStatusQueryMock.mock.calls.length).toBe(3);
    const polledAgents = sendStatusQueryMock.mock.calls.map((c) => c[1]);
    expect(polledAgents).toContain('worker');
    expect(polledAgents).toContain('coordinator');
  });

  it('keeps polling on the 8s interval (does not disable the auto-poll feature)', () => {
    render(<GraphView />);
    expect(sendStatusQueryMock.mock.calls.length).toBe(1);

    // Advance 8s — exactly one interval tick.
    act(() => {
      vi.advanceTimersByTime(8_000);
    });
    expect(sendStatusQueryMock.mock.calls.length).toBe(2);

    // Advance another 8s — another tick.
    act(() => {
      vi.advanceTimersByTime(8_000);
    });
    expect(sendStatusQueryMock.mock.calls.length).toBe(3);
  });
});
