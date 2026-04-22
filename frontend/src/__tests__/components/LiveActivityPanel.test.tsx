// Covers harmonograf#89: the Live Activity header must clear once the
// session is no longer LIVE. Without this, INVOCATION spans that never got
// a clean endMs (server shut down mid-run, agent process exited without
// flushing) stay "RUNNING" forever and the header reports N running agents
// on a session the user has already confirmed is done.

import { act, render, screen } from '@testing-library/react';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Agent, Span, SpanKind, SpanStatus } from '../../gantt/types';

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

vi.mock('../../rpc/hooks', async () => {
  // Preserve the real sessionIsInactive helper; only mock the hooks that
  // touch the live RPC client.
  const actual =
    await vi.importActual<typeof import('../../rpc/hooks')>(
      '../../rpc/hooks',
    );
  return {
    ...actual,
    getSessionStore: (id: string | null) => (id ? mockStore : undefined),
    useSessionWatch: () => mockWatch ?? null,
  };
});

vi.mock('../../state/uiStore', () => {
  const state = {
    currentSessionId: 'session-1',
    liveActivityCollapsed: false,
    toggleLiveActivity: () => {},
    selectSpan: () => {},
  };
  return {
    useUiStore: <T,>(selector: (s: typeof state) => T) => {
      // Keep the selector pointed at the latest mockSessionId so a test
      // that clears it reflects immediately.
      return selector({ ...state, currentSessionId: mockSessionId ?? '' });
    },
  };
});

import { LiveActivityPanel } from '../../components/LiveActivity/LiveActivityPanel';

function agent(id: string, name = id): Agent {
  return {
    id,
    name,
    framework: 'ADK',
    capabilities: [],
    status: 'CONNECTED',
    connectedAtMs: 0,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
  };
}

function span(
  id: string,
  agentId: string,
  kind: SpanKind,
  opts: { endMs?: number | null; status?: SpanStatus; startMs?: number } = {},
): Span {
  return {
    id,
    sessionId: 'session-1',
    agentId,
    parentSpanId: null,
    kind,
    name: 'work',
    status: opts.status ?? 'RUNNING',
    startMs: opts.startMs ?? 0,
    endMs: opts.endMs ?? null,
    links: [],
    attributes: {},
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
}

function baseWatch(overrides: Partial<WatchMock> = {}): WatchMock {
  return {
    store: mockStore!,
    connected: true,
    initialBurstComplete: true,
    error: null,
    sessionStatus: 'LIVE',
    lastEventAtMs: Date.now(),
    ...overrides,
  };
}

describe('<LiveActivityPanel /> stale-state fix (harmonograf#89)', () => {
  beforeEach(() => {
    mockStore = new SessionStore();
    mockSessionId = 'session-1';
    // Seed two agents and a still-open INVOCATION for each — exactly the
    // shape that leaves "2 RUNNING" stuck on a completed session.
    mockStore.agents.upsert(agent('coordinator_agent', 'coordinator'));
    mockStore.agents.upsert(agent('web_developer_agent', 'web_developer'));
    mockStore.spans.append(
      span('inv-coord', 'coordinator_agent', 'INVOCATION', { startMs: 100 }),
    );
    mockStore.spans.append(
      span('inv-web', 'web_developer_agent', 'INVOCATION', { startMs: 200 }),
    );
    mockWatch = baseWatch();
  });
  afterEach(() => {
    mockStore = undefined;
    mockWatch = undefined;
  });

  it('shows "2 running" while the session is LIVE', () => {
    mockWatch = baseWatch({ sessionStatus: 'LIVE' });
    render(<LiveActivityPanel />);
    expect(screen.getByText(/2 running/i)).toBeInTheDocument();
  });

  it('clears the header when the session has transitioned to COMPLETED', () => {
    mockWatch = baseWatch({ sessionStatus: 'COMPLETED' });
    render(<LiveActivityPanel />);
    // The panel flips to the empty "No active work" state rather than
    // listing stale INVOCATIONs.
    expect(screen.queryByText(/running/i)).toBeNull();
    expect(screen.getByText(/no active work/i)).toBeInTheDocument();
  });

  it('clears the header on ABORTED sessions too', () => {
    mockWatch = baseWatch({ sessionStatus: 'ABORTED' });
    render(<LiveActivityPanel />);
    expect(screen.queryByText(/running/i)).toBeNull();
    expect(screen.getByText(/no active work/i)).toBeInTheDocument();
  });

  it('clears via the inactivity-grace fallback when status never transitioned', () => {
    // Server forgot to emit SessionEnded, but the stream has been quiet
    // for a long time and we're disconnected — treat as inactive.
    const longAgo = Date.now() - 5 * 60_000;
    mockWatch = baseWatch({
      sessionStatus: 'UNKNOWN',
      connected: false,
      lastEventAtMs: longAgo,
    });
    // The panel samples wall-clock inside a setInterval callback (to keep
    // the render body pure). Advance fake timers past the first tick so
    // nowWallMs populates with a real value, then the staleness heuristic
    // fires. Without this, the initial render sees nowWallMs=0 and can't
    // evaluate the fallback.
    vi.useFakeTimers({ now: Date.now() });
    try {
      render(<LiveActivityPanel />);
      act(() => {
        vi.advanceTimersByTime(1100);
      });
      expect(screen.queryByText(/running/i)).toBeNull();
      expect(screen.getByText(/no active work/i)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
