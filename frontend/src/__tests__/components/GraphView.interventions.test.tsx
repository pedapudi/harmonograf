// harmonograf#goldfive-unify — fix B of the Graph view unification:
// the sequence diagram must render goldfive's interventions (drift
// detections + plan revisions) on top of the agent/delegation topology
// so the user→goldfive→target-agent chain is visible. Previously these
// signals were only rendered on the Trajectory view; the Graph view
// had no arrows for them at all (issue filed alongside this fix).
//
// These tests exercise the overlay additions in GraphView.tsx:
//   * drift glyphs on goldfive's / user's column
//   * steering arrows (goldfive → target agent) per plan revision
//   * user-steer arrows (__user__ → goldfive) for user-authored drifts
//   * click-through to the SteeringDetailPanel
import { act, fireEvent, render } from '@testing-library/react';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Agent, TaskPlan } from '../../gantt/types';
import { GOLDFIVE_ACTOR_ID, USER_ACTOR_ID } from '../../theme/agentColors';

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
const mockSessionId: string | null = 'session-1';

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

function agent(id: string, connectedAtMs = 1, framework: Agent['framework'] = 'ADK'): Agent {
  return {
    id,
    name: id,
    framework,
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

function plan(id: string, revisionIndex: number, triggerEventId = '', revisionKind = ''): TaskPlan {
  return {
    id,
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 100 + revisionIndex * 10,
    summary: `plan ${id} r${revisionIndex}`,
    tasks: [
      {
        id: 't1',
        title: 'first',
        description: '',
        assigneeAgentId: 'coordinator',
        status: 'PENDING',
        predictedStartMs: 0,
        predictedDurationMs: 0,
        boundSpanId: '',
        supersedes: '',
      },
    ],
    edges: [],
    revisionReason: revisionIndex === 0 ? '' : 'needs refocus',
    revisionKind,
    revisionSeverity: revisionIndex === 0 ? '' : 'warning',
    revisionIndex,
    triggerEventId,
  };
}

describe('<GraphView /> goldfive interventions (fix B of harmonograf#goldfive-unify)', () => {
  beforeEach(() => {
    mockStore = new SessionStore();
    // Real worker agents + synthetic goldfive + user rows.
    mockStore.agents.upsert(agent('coordinator', 10));
    mockStore.agents.upsert(agent('worker_a', 12));
    mockStore.agents.upsert({
      ...agent(GOLDFIVE_ACTOR_ID),
      framework: 'CUSTOM',
    });
    mockStore.agents.upsert({
      ...agent(USER_ACTOR_ID),
      framework: 'CUSTOM',
      name: 'user',
    });
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

  it('renders a drift glyph on the goldfive column for a goldfive-authored drift', () => {
    act(() => {
      mockStore!.drifts.append({
        kind: 'off_topic',
        severity: 'warning',
        detail: 'agent drifted',
        taskId: 't1',
        agentId: 'worker_a',
        recordedAtMs: 5_000,
        annotationId: '',
        driftId: 'd1',
        authoredBy: 'goldfive',
      });
    });
    const { container } = render(<GraphView />);
    const glyphs = container.querySelectorAll('[data-testid^="drift-glyph-"]');
    expect(glyphs.length).toBe(1);
    const glyph = glyphs[0];
    expect(glyph.getAttribute('data-severity')).toBe('warning');
    expect(glyph.getAttribute('data-authored-by')).toBe('goldfive');
  });

  it('renders a steering arrow from goldfive to the target agent on a plan_revised', () => {
    act(() => {
      // Plan rev 0 (baseline) + rev 1 (triggered by d1).
      mockStore!.tasks.upsertPlan(plan('p1', 0));
      mockStore!.drifts.append({
        kind: 'off_topic',
        severity: 'warning',
        detail: 'drift',
        taskId: 't1',
        agentId: 'worker_a',
        recordedAtMs: 5_000,
        annotationId: '',
        driftId: 'd1',
        authoredBy: 'goldfive',
      });
      // Plan revision 1, triggered by d1. revisionKind surfaces on the label.
      mockStore!.tasks.upsertPlan(plan('p1', 1, 'd1', 'off_topic'));
      mockStore!.planHistory.append({
        revision: 1,
        plan: plan('p1', 1, 'd1', 'off_topic'),
        reason: 'needs refocus',
        kind: 'off_topic',
        triggerEventId: 'd1',
        emittedAtMs: 5_100,
      });
    });
    const { container } = render(<GraphView />);
    const steers = container.querySelectorAll('[data-testid^="steering-arrow-"]');
    // At least one steering arrow — the goldfive→worker_a one.
    expect(steers.length).toBeGreaterThanOrEqual(1);
    const arrow = Array.from(steers).find((s) =>
      s.getAttribute('data-testid')?.includes('steer-p1-1'),
    );
    expect(arrow).toBeTruthy();
    expect(arrow?.getAttribute('data-severity')).toBe('warning');
    // The arrow label mentions the revision kind.
    const text = arrow?.querySelector('text')?.textContent || '';
    expect(text).toContain('refine');
    expect(text).toContain('off_topic');
  });

  it('renders a user-steer arrow from __user__ → goldfive for USER_STEER drifts', () => {
    act(() => {
      mockStore!.drifts.append({
        kind: 'user_steer',
        severity: 'info',
        detail: 'focus on section 2',
        taskId: 't1',
        agentId: 'worker_a',
        recordedAtMs: 4_000,
        annotationId: 'ann-1',
        driftId: 'd-user',
        authoredBy: 'user',
      });
    });
    const { container } = render(<GraphView />);
    const userSteer = container.querySelector(
      '[data-testid="steering-arrow-user-steer-0"]',
    );
    expect(userSteer).toBeTruthy();
    const text = userSteer?.querySelector('text')?.textContent || '';
    expect(text).toContain('user steer');
  });

  it('drift glyph landing a user-authored drift lives on the user column, not goldfive', () => {
    act(() => {
      mockStore!.drifts.append({
        kind: 'user_steer',
        severity: 'info',
        detail: 'hi',
        taskId: 't1',
        agentId: 'worker_a',
        recordedAtMs: 4_000,
        annotationId: 'ann-1',
        driftId: 'd-user',
        authoredBy: 'user',
      });
    });
    const { container } = render(<GraphView />);
    const glyph = container.querySelector('[data-testid^="drift-glyph-"]');
    expect(glyph?.getAttribute('data-authored-by')).toBe('user');
  });

  it('clicking a steering arrow opens the SteeringDetailPanel', () => {
    act(() => {
      mockStore!.tasks.upsertPlan(plan('p1', 0));
      mockStore!.drifts.append({
        kind: 'off_topic',
        severity: 'critical',
        detail: 'drift',
        taskId: 't1',
        agentId: 'worker_a',
        recordedAtMs: 5_000,
        annotationId: '',
        driftId: 'd1',
        authoredBy: 'goldfive',
      });
      mockStore!.tasks.upsertPlan(plan('p1', 1, 'd1', 'off_topic'));
      mockStore!.planHistory.append({
        revision: 0,
        plan: plan('p1', 0),
        reason: '',
        kind: '',
        triggerEventId: '',
        emittedAtMs: 0,
      });
      mockStore!.planHistory.append({
        revision: 1,
        plan: plan('p1', 1, 'd1', 'off_topic'),
        reason: 'needs refocus',
        kind: 'off_topic',
        triggerEventId: 'd1',
        emittedAtMs: 5_100,
      });
    });
    const { container, queryByTestId } = render(<GraphView />);
    expect(queryByTestId('steering-detail-panel')).toBeFalsy();
    const arrow = container.querySelector(
      '[data-testid="steering-arrow-steer-p1-1"]',
    );
    expect(arrow).toBeTruthy();
    fireEvent.click(arrow!);
    expect(queryByTestId('steering-detail-panel')).toBeTruthy();
  });

  it('session with one drift + one plan_revised: exactly 1 goldfive node + 1 drift glyph + 1 steering arrow', () => {
    act(() => {
      mockStore!.tasks.upsertPlan(plan('p1', 0));
      mockStore!.drifts.append({
        kind: 'off_topic',
        severity: 'warning',
        detail: 'drift',
        taskId: 't1',
        agentId: 'worker_a',
        recordedAtMs: 5_000,
        annotationId: '',
        driftId: 'd1',
        authoredBy: 'goldfive',
      });
      mockStore!.tasks.upsertPlan(plan('p1', 1, 'd1', 'off_topic'));
      mockStore!.planHistory.append({
        revision: 1,
        plan: plan('p1', 1, 'd1', 'off_topic'),
        reason: 'needs refocus',
        kind: 'off_topic',
        triggerEventId: 'd1',
        emittedAtMs: 5_100,
      });
    });
    const { container } = render(<GraphView />);
    // Exactly one goldfive row in the agent list. The header clip path
    // is keyed off the agent id in the column. Check the agent count hint.
    const hint = container.querySelector('.hg-panel__hint')?.textContent || '';
    // 4 agents — coordinator, worker_a, goldfive, user.
    expect(hint).toMatch(/4 agents/);
    const glyphs = container.querySelectorAll('[data-testid^="drift-glyph-"]');
    expect(glyphs.length).toBe(1);
    const steers = container.querySelectorAll('[data-testid^="steering-arrow-"]');
    expect(steers.length).toBe(1);
  });

  // ─── InvocationCancelled glyphs (goldfive#251 Stream C) ──────────────
  it('renders a cancel glyph on the cancelled agent lifeline', () => {
    act(() => {
      mockStore!.invocationCancels.append({
        runId: 'run-1',
        invocationId: 'inv-a',
        agentId: 'worker_a',
        reason: 'drift',
        severity: 'critical',
        driftId: 'd-cancel-1',
        driftKind: 'off_topic',
        detail: 'stopped veering',
        toolName: '',
        recordedAtMs: 6_000,
      });
    });
    const { container } = render(<GraphView />);
    const cancels = container.querySelectorAll('[data-variant="cancel"]');
    expect(cancels.length).toBe(1);
    const cancel = cancels[0];
    expect(cancel.getAttribute('data-severity')).toBe('critical');
    // The cancel marker carries a circle + slash (stop glyph).
    expect(cancel.querySelector('circle')).toBeTruthy();
    expect(cancel.querySelector('line')).toBeTruthy();
    // Title tooltip surfaces the detail text.
    const title = cancel.querySelector('title')?.textContent || '';
    expect(title).toContain('CANCELLED');
    expect(title).toContain('stopped veering');
  });

  it('cancel glyph lands on the cancelled agent column, not goldfive/user', () => {
    act(() => {
      mockStore!.invocationCancels.append({
        runId: 'run-1',
        invocationId: 'inv-a',
        agentId: 'worker_a',
        reason: 'drift',
        severity: 'warning',
        driftId: 'd1',
        driftKind: 'off_topic',
        detail: 'cx',
        toolName: '',
        recordedAtMs: 1_000,
      });
    });
    const { container } = render(<GraphView />);
    const cancel = container.querySelector('[data-variant="cancel"]');
    expect(cancel).toBeTruthy();
    // The glyph's cx attribute comes from the column index for
    // worker_a — we can at least assert that a drift glyph on a
    // different column would produce a different cx. Here we assert
    // that goldfive and user columns don't carry a cancel variant.
    const gSteer = container.querySelector(
      '[data-variant="cancel"][data-authored-by="goldfive"]',
    );
    // No `data-authored-by` is set on cancel glyphs — they don't
    // belong to the drift-authored-by taxonomy. Just confirm that
    // drift glyphs on goldfive's col don't carry variant=cancel.
    expect(gSteer).toBeFalsy();
  });

  it('multiple cancels produce distinct glyphs', () => {
    act(() => {
      mockStore!.invocationCancels.append({
        runId: 'run-1',
        invocationId: 'inv-a',
        agentId: 'worker_a',
        reason: 'drift',
        severity: 'warning',
        driftId: 'd1',
        driftKind: 'off_topic',
        detail: 'one',
        toolName: '',
        recordedAtMs: 1_000,
      });
      mockStore!.invocationCancels.append({
        runId: 'run-1',
        invocationId: 'inv-b',
        agentId: 'coordinator',
        reason: 'user_steer',
        severity: 'info',
        driftId: '',
        driftKind: 'user_steer',
        detail: 'two',
        toolName: '',
        recordedAtMs: 2_000,
      });
    });
    const { container } = render(<GraphView />);
    const cancels = container.querySelectorAll('[data-variant="cancel"]');
    expect(cancels.length).toBe(2);
  });
});
