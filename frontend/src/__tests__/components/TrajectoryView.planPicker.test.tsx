/**
 * Test for the Trajectory view's plan picker chip-bar (multi-plan
 * sessions). Covers:
 *   * picker hidden on single-plan sessions,
 *   * picker rendered with one chip per plan on multi-plan sessions,
 *   * default selection lands on the latest plan by createdAt,
 *   * clicking a chip switches the active plan and updates the
 *     "rev N of M" header to that plan's revision range,
 *   * persisted selection (uiStore.trajectorySelectedPlanId) survives
 *     a remount.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Task, TaskPlan } from '../../gantt/types';

vi.mock('../../components/shell/views/views.css', () => ({}));

let mockStore = new SessionStore();
const mockSessionId = 'sess-plan-picker';

vi.mock('../../rpc/hooks', () => ({
  useSessionWatch: () => ({
    store: mockStore,
    connected: true,
    initialBurstComplete: true,
    error: null,
    sessionStatus: 'LIVE' as const,
    lastEventAtMs: Date.now(),
  }),
  getSessionStore: () => mockStore,
}));

const uiStoreState = {
  currentSessionId: mockSessionId as string | null,
  selectSpan: vi.fn(),
  trajectoryLegacyExpanded: false,
  toggleTrajectoryLegacyExpanded: (): void => {
    uiStoreState.trajectoryLegacyExpanded =
      !uiStoreState.trajectoryLegacyExpanded;
  },
  selectedRevision: null as number | null,
  setSelectedRevision: (rev: number | null): void => {
    uiStoreState.selectedRevision = rev;
  },
  trajectorySelectedPlanId: null as string | null,
  setTrajectorySelectedPlanId: (id: string | null): void => {
    uiStoreState.trajectorySelectedPlanId = id;
  },
};
vi.mock('../../state/uiStore', () => ({
  useUiStore: <T,>(selector: (s: typeof uiStoreState) => T) =>
    selector(uiStoreState),
}));

vi.mock('../../state/annotationStore', () => ({
  useAnnotationStore: Object.assign(() => ({ list: () => [] }), {
    getState: () => ({ list: () => [] }),
    subscribe: () => () => {},
  }),
}));

import { TrajectoryView } from '../../components/shell/views/TrajectoryView';

function mkTask(id: string, assignee = 'agent-a'): Task {
  return {
    id,
    title: id,
    description: '',
    assigneeAgentId: assignee,
    status: 'PENDING',
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    cancelReason: '',
    supersedes: '',
  };
}

function mkPlan(
  id: string,
  tasks: Task[],
  createdAtMs: number,
  revisionIndex = 0,
  summary = '',
): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}-${revisionIndex}`,
    plannerAgentId: 'planner-agent',
    createdAtMs,
    summary: summary || `plan ${id}`,
    tasks,
    edges: [],
    revisionReason: '',
    revisionKind: '',
    revisionSeverity: '',
    revisionIndex,
    triggerEventId: '',
  };
}

function headerHint(): string {
  return (
    document.querySelector('.hg-traj__header .hg-panel__hint')?.textContent ?? ''
  );
}

beforeEach(() => {
  mockStore = new SessionStore();
  uiStoreState.currentSessionId = mockSessionId;
  uiStoreState.trajectoryLegacyExpanded = false;
  uiStoreState.selectedRevision = null;
  uiStoreState.trajectorySelectedPlanId = null;
});
afterEach(() => {
  vi.clearAllMocks();
});

describe('<TrajectoryView /> plan picker', () => {
  it('does not render the picker on single-plan sessions', () => {
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 100, 0));
    render(<TrajectoryView />);
    expect(screen.queryByTestId('trajectory-plan-picker')).toBeNull();
  });

  it('renders a chip per plan on a multi-plan session', () => {
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 100, 0, 'first'));
    mockStore.tasks.upsertPlan(mkPlan('p2', [mkTask('t2')], 200, 0, 'second'));
    mockStore.tasks.upsertPlan(mkPlan('p3', [mkTask('t3')], 300, 0, 'third'));
    render(<TrajectoryView />);
    expect(screen.getByTestId('trajectory-plan-picker')).toBeInTheDocument();
    expect(screen.getByTestId('plan-chip-p1')).toBeInTheDocument();
    expect(screen.getByTestId('plan-chip-p2')).toBeInTheDocument();
    expect(screen.getByTestId('plan-chip-p3')).toBeInTheDocument();
  });

  it('defaults the selection to the latest plan by createdAt', () => {
    // p2 has the highest createdAtMs ⇒ it's the default.
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 100, 0));
    mockStore.tasks.upsertPlan(
      mkPlan('p2', [mkTask('t2')], 300, 0),
    );
    mockStore.tasks.upsertPlan(mkPlan('p3', [mkTask('t3')], 200, 0));
    render(<TrajectoryView />);
    expect(screen.getByTestId('plan-chip-p2').getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('plan-chip-p1').getAttribute('aria-selected')).toBe('false');
    expect(screen.getByTestId('plan-chip-p3').getAttribute('aria-selected')).toBe('false');
  });

  it('switches active plan on chip click and updates the rev-of header', () => {
    // p1 has revs 0, 1; p2 has revs 0, 1, 2.
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('a')], 100, 0));
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('a'), mkTask('b')], 110, 1));
    mockStore.tasks.upsertPlan(mkPlan('p2', [mkTask('x')], 200, 0));
    mockStore.tasks.upsertPlan(mkPlan('p2', [mkTask('x'), mkTask('y')], 210, 1));
    mockStore.tasks.upsertPlan(
      mkPlan('p2', [mkTask('x'), mkTask('y'), mkTask('z')], 220, 2),
    );
    const { rerender } = render(<TrajectoryView />);
    // Default = latest plan = p2 ⇒ "rev 2 of 2" (highest rev = 2).
    expect(headerHint()).toBe('rev 2 of 2');
    // Click p1's chip.
    fireEvent.click(screen.getByTestId('plan-chip-p1'));
    rerender(<TrajectoryView />);
    // p1's max revisionIndex is 1 ⇒ "rev 1 of 1".
    expect(headerHint()).toBe('rev 1 of 1');
    expect(uiStoreState.trajectorySelectedPlanId).toBe('p1');
  });

  it('persists selection across re-render via uiStore', () => {
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('a')], 100, 0));
    mockStore.tasks.upsertPlan(mkPlan('p2', [mkTask('b')], 200, 0));
    // Pre-set the persisted choice ⇒ render should pick p1, not p2.
    uiStoreState.trajectorySelectedPlanId = 'p1';
    render(<TrajectoryView />);
    expect(screen.getByTestId('plan-chip-p1').getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('plan-chip-p2').getAttribute('aria-selected')).toBe('false');
  });

  it('falls back to latest when persisted plan id is no longer in the session', () => {
    // Persisted id refers to a plan not in this session ⇒ fall back to
    // the latest plan instead of rendering nothing selected.
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('a')], 100, 0));
    mockStore.tasks.upsertPlan(mkPlan('p2', [mkTask('b')], 200, 0));
    uiStoreState.trajectorySelectedPlanId = 'stale-plan-id';
    render(<TrajectoryView />);
    expect(screen.getByTestId('plan-chip-p2').getAttribute('aria-selected')).toBe('true');
  });
});
