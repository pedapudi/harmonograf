// harmonograf#107 — the sequence diagram view must draw delegation arrows
// between agents when goldfive's observer emits a DelegationObserved event.
// Coordinator→AgentTool sub-agent invocations produce span trees whose
// parent pointer is same-agent (the coordinator row carries a TOOL_CALL
// wrapping the sub-agent's INVOCATION), so the Method-2 cross-agent-parent
// inference in computeSequence misses them. Method 3 (this test) feeds the
// arrow directly from store.delegations.
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
import type { Agent } from '../../gantt/types';

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
  const actual =
    await vi.importActual<typeof import('../../rpc/hooks')>(
      '../../rpc/hooks',
    );
  return {
    ...actual,
    getSessionStore: (id: string | null) => (id ? mockStore : undefined),
    useSessionWatch: () => mockWatch ?? null,
    sendStatusQuery: vi.fn().mockResolvedValue(undefined),
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

describe('<GraphView /> delegation arrows (#107)', () => {
  beforeEach(() => {
    mockStore = new SessionStore();
    mockSessionId = 'session-1';
    // Two agents: coordinator + sub_agent. No cross-agent span parent — the
    // only signal the sub-agent got delegated work is the DelegationObserved
    // event below.
    mockStore.agents.upsert(agent('coordinator', 1));
    mockStore.agents.upsert(agent('sub_agent', 2));
    mockWatch = {
      store: mockStore,
      connected: true,
      initialBurstComplete: true,
      error: null,
      sessionStatus: 'COMPLETED',
      lastEventAtMs: 0,
    };
  });

  afterEach(() => {
    mockStore = undefined;
    mockWatch = undefined;
  });

  it('renders a delegation arrow marker when a DelegationObserved fires', () => {
    act(() => {
      mockStore!.delegations.append({
        fromAgentId: 'coordinator',
        toAgentId: 'sub_agent',
        taskId: 'task-1',
        invocationId: 'inv-1',
        observedAtMs: 5_000,
      });
    });
    const { container } = render(<GraphView />);
    // Delegation arrows use marker #arr-delegation — assert at least one
    // <line> references it. (Transfer and return arrows use different
    // markers, so this specifically validates the delegation code path.)
    const delegLines = Array.from(
      container.querySelectorAll('line[marker-end]'),
    ).filter((n) => n.getAttribute('marker-end') === 'url(#arr-delegation)');
    expect(delegLines.length).toBeGreaterThanOrEqual(1);
  });

  it('skips self-delegations and unknown agents', () => {
    act(() => {
      // Self-delegation — must NOT produce an arrow.
      mockStore!.delegations.append({
        fromAgentId: 'coordinator',
        toAgentId: 'coordinator',
        taskId: '',
        invocationId: 'inv-self',
        observedAtMs: 1_000,
      });
      // Unknown agent on the "to" side — guarded by the column-index
      // lookup in computeSequence.
      mockStore!.delegations.append({
        fromAgentId: 'coordinator',
        toAgentId: 'not_registered',
        taskId: '',
        invocationId: 'inv-unknown',
        observedAtMs: 2_000,
      });
    });
    const { container } = render(<GraphView />);
    const delegLines = Array.from(
      container.querySelectorAll('line[marker-end]'),
    ).filter((n) => n.getAttribute('marker-end') === 'url(#arr-delegation)');
    expect(delegLines.length).toBe(0);
  });

  it('re-renders when a new delegation arrives post-mount', () => {
    const { container } = render(<GraphView />);
    // Initially no delegations — no delegation arrow.
    let delegLines = Array.from(
      container.querySelectorAll('line[marker-end]'),
    ).filter((n) => n.getAttribute('marker-end') === 'url(#arr-delegation)');
    expect(delegLines.length).toBe(0);

    act(() => {
      mockStore!.delegations.append({
        fromAgentId: 'coordinator',
        toAgentId: 'sub_agent',
        taskId: 'task-1',
        invocationId: 'inv-1',
        observedAtMs: 3_000,
      });
    });

    delegLines = Array.from(
      container.querySelectorAll('line[marker-end]'),
    ).filter((n) => n.getAttribute('marker-end') === 'url(#arr-delegation)');
    expect(delegLines.length).toBeGreaterThanOrEqual(1);
  });
});
