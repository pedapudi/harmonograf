/**
 * harmonograf#197: TrajectoryView plan-evolution + goldfive-steering redesign.
 *
 * Covers the eight tier-1 behaviors specified in the issue:
 *
 *  1. Cumulative DAG — superseded tasks retained as historical nodes.
 *  2. Superseded styling — muted opacity, border dashed, still clickable.
 *  3. Generation badge — `REV N` corner badge derived from taskRevisionMeta.
 *  4. Goldfive steering arrow — dashed goldfive-colored arrow from a gutter
 *     node to the replacement task, labeled with kind → target-agent.
 *  5. User-steer arrow — distinct purple-dashed style + "user steer" label.
 *  6. Arrowhead endpoint — lands on the target agent's task node (the
 *     `data-testid="steer-edge-<taskId>"` attribute is our asserted anchor).
 *  7. Supersedes edge — old → new, dashed muted, annotated with kind.
 *  8. Revision scrubber — filters out tasks introduced after the pinned rev.
 *  9. Steering detail panel — opens on click with Trigger / Steering / Target.
 */

import { render, screen, fireEvent, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Task, TaskPlan } from '../../gantt/types';

vi.mock('../../components/shell/views/views.css', () => ({}));

let mockStore = new SessionStore();
const mockSessionId: string = 'sess-evo';

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
  // Restructure: legacy stacked sections are opt-in. Default `false` so
  // the tests exercise the new unified ribbon + floating drawer layout.
  // Individual tests flip this via `setLegacyExpanded(true)` to exercise
  // the escape hatch.
  trajectoryLegacyExpanded: false,
  toggleTrajectoryLegacyExpanded: (): void => {
    uiStoreState.trajectoryLegacyExpanded = !uiStoreState.trajectoryLegacyExpanded;
  },
};
function setLegacyExpanded(v: boolean): void {
  uiStoreState.trajectoryLegacyExpanded = v;
}
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

// ── fixtures ───────────────────────────────────────────────────────────────

function mkTask(
  id: string,
  status: Task['status'],
  assignee = 'agent-a',
  title = id,
): Task {
  return {
    id,
    title,
    description: '',
    assigneeAgentId: assignee,
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    cancelReason: '',
  };
}

function mkPlan(
  id: string,
  tasks: Task[],
  revisionIndex = 0,
  revisionReason = '',
  revisionKind = '',
  triggerEventId = '',
): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}`,
    plannerAgentId: 'planner-agent',
    createdAtMs: revisionIndex,
    summary: `plan ${id}`,
    tasks,
    edges: [],
    revisionReason,
    revisionKind,
    revisionSeverity: 'warning',
    revisionIndex,
    triggerEventId,
  };
}

// Seed two revisions and the supersession event so the cumulative +
// supersedes + steering paths all fire. Shared by several tests below.
function seedTwoRevsWithReplacement(): {
  rev0: TaskPlan;
  rev1: TaskPlan;
} {
  const rev0 = mkPlan('p1', [
    mkTask('t1', 'COMPLETED', 'agent-a', 'build outline'),
  ]);
  mockStore.tasks.upsertPlan(rev0);
  const rev1 = mkPlan(
    'p1',
    [mkTask('t1_corrected', 'PENDING', 'research_agent', 'research paper')],
    1,
    'coordinator looped on status queries',
    'off_topic',
    'drift-42',
  );
  mockStore.tasks.upsertPlan(rev1);
  mockStore.agents.upsert({
    id: '__goldfive__',
    name: 'goldfive',
    framework: 'CUSTOM',
    capabilities: [],
    status: 'CONNECTED',
    connectedAtMs: 1,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
  });
  mockStore.agents.upsert({
    id: 'research_agent',
    name: 'research_agent',
    framework: 'ADK',
    capabilities: [],
    status: 'CONNECTED',
    connectedAtMs: 1,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
  });
  // Seed a drift so the "Jump to Gantt" deep-link has a triggerDriftAtMs.
  mockStore.drifts.append({
    kind: 'off_topic',
    severity: 'warning',
    detail: 'coordinator looped on status queries',
    taskId: 't1',
    agentId: 'agent-a',
    recordedAtMs: 10,
    annotationId: '',
    driftId: 'drift-42',
  });
  return { rev0, rev1 };
}

beforeEach(() => {
  mockStore = new SessionStore();
  uiStoreState.currentSessionId = mockSessionId;
  uiStoreState.trajectoryLegacyExpanded = false;
});
afterEach(() => {
  vi.clearAllMocks();
});

describe('<TrajectoryView /> plan-evolution + steering', () => {
  it('renders both generations of tasks in the cumulative DAG', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    // Rev 0's task AND rev 1's replacement task are both rendered.
    expect(screen.getByTestId('task-node-t1')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-t1_corrected')).toBeInTheDocument();
  });

  it('marks the superseded task with reduced opacity and keeps it clickable', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const superseded = screen.getByTestId('task-node-t1');
    expect(superseded).toHaveAttribute('data-superseded', 'true');
    expect(superseded.getAttribute('opacity')).toBe('0.4');
    // The node is still a click target — the onClick-bearing <g> receives
    // pointer events (cursor: pointer via .hg-traj__node) even when the
    // task is superseded. Clicking routes through the new drawer: the
    // cumulative plan still carries the superseded task, so the task
    // detail body renders.
    fireEvent.click(superseded);
    expect(screen.getByTestId('trajectory-drawer')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-detail')).toHaveTextContent('build outline');
  });

  it('stamps a REV N generation badge based on introducedInRevision', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const rev0Badge = screen.getByTestId('task-node-t1-rev-badge');
    expect(rev0Badge).toHaveAttribute('data-rev', '0');
    expect(rev0Badge).toHaveTextContent('R0');
    const rev1Badge = screen.getByTestId('task-node-t1_corrected-rev-badge');
    expect(rev1Badge).toHaveAttribute('data-rev', '1');
    expect(rev1Badge).toHaveTextContent('R1');
  });

  it('draws a goldfive steering arrow to the replacement task with kind → agent label', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const edges = screen.getByTestId('trajectory-steer-edges');
    expect(edges).toBeInTheDocument();
    // The arrowhead lands ON the replacement task node — asserted via the
    // data-testid that the steering-arrow render attaches to the group
    // that terminates at the target task node.
    const arrow = screen.getByTestId('steer-edge-t1_corrected');
    expect(arrow).toHaveAttribute('data-authored-by', 'goldfive');
    // The short tail label names the target agent + kind.
    expect(arrow).toHaveTextContent(/OFF_TOPIC/i);
    expect(arrow).toHaveTextContent(/research_agent/);
  });

  it('draws a user-styled arrow for user_steer drifts', () => {
    // rev 0
    const rev0 = mkPlan('p1', [mkTask('t1', 'RUNNING', 'agent-a')]);
    mockStore.tasks.upsertPlan(rev0);
    // rev 1 — originated by the user via a STEER annotation. Goldfive
    // stamps `kind=user_steer` on the PlanRevised.
    const rev1 = mkPlan(
      'p1',
      [mkTask('t1', 'RUNNING', 'agent-a'), mkTask('t2', 'PENDING', 'agent-b')],
      1,
      'switch focus to section 3',
      'user_steer',
      'ann-7',
    );
    mockStore.tasks.upsertPlan(rev1);
    render(<TrajectoryView />);
    const arrow = screen.getByTestId('steer-edge-t2');
    expect(arrow).toHaveAttribute('data-authored-by', 'user');
    expect(arrow).toHaveTextContent(/user steer/i);
    expect(arrow).toHaveTextContent(/agent-b/);
  });

  it('draws a supersedes edge from the old task to its replacement, annotated with drift kind', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const supersedes = screen.getByTestId('trajectory-supersedes-edges');
    expect(supersedes).toBeInTheDocument();
    const edge = screen.getByTestId('supersedes-edge-t1');
    expect(edge).toHaveTextContent(/goldfive: off_topic/i);
  });

  it('unified ribbon hides tasks introduced after the pinned revision', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    // Before scrubbing: both tasks visible.
    expect(screen.getByTestId('task-node-t1')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-t1_corrected')).toBeInTheDocument();
    // Click the REV 0 ribbon notch (now the sole revision-pinning control
    // in the default layout).
    const notch = screen.getByTestId('ribbon-rev-0');
    fireEvent.click(notch);
    // After scrubbing to rev 0: rev-1 task is hidden.
    expect(screen.getByTestId('task-node-t1')).toBeInTheDocument();
    expect(screen.queryByTestId('task-node-t1_corrected')).not.toBeInTheDocument();
    // "Latest" returns the full cumulative view.
    fireEvent.click(screen.getByTestId('ribbon-latest-btn'));
    expect(screen.getByTestId('task-node-t1_corrected')).toBeInTheDocument();
  });

  it('clicking a steering arrow opens the floating drawer with Trigger / Steering / Target body', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const arrow = screen.getByTestId('steer-edge-t1_corrected');
    fireEvent.click(arrow);
    const drawer = screen.getByTestId('trajectory-drawer');
    expect(drawer).toBeInTheDocument();
    const body = within(drawer).getByTestId('steering-detail-body');
    // Three sections: Trigger, Steering, Target.
    expect(within(body).getByTestId('steering-detail-trigger')).toBeInTheDocument();
    expect(within(body).getByTestId('steering-detail-steering')).toBeInTheDocument();
    const targetSec = within(body).getByTestId('steering-detail-target');
    // Target section names the agent explicitly — the primary UX guarantee.
    expect(
      within(targetSec).getByTestId('steering-detail-target-agent'),
    ).toHaveTextContent('research_agent');
    expect(
      within(targetSec).getByTestId('steering-detail-target-task'),
    ).toHaveTextContent(/research paper/);
    // Kind + reason surface in the Trigger section.
    expect(
      within(body).getByTestId('steering-detail-trigger'),
    ).toHaveTextContent('off_topic');
    expect(
      within(body).getByTestId('steering-detail-trigger'),
    ).toHaveTextContent('coordinator looped on status queries');
  });

  it('clicking a supersedes edge opens the drawer with old / new task pair in body', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const edge = screen.getByTestId('supersedes-edge-t1');
    // SVG-native click — fireEvent dispatches to the parent <g>.
    fireEvent.click(edge);
    const drawer = screen.getByTestId('trajectory-drawer');
    expect(drawer).toBeInTheDocument();
    const body = within(drawer).getByTestId('steering-detail-body');
    expect(body).toHaveTextContent('t1');
    expect(body).toHaveTextContent('t1_corrected');
  });

  it('drawer close button dismisses the drawer, backdrop click also closes', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    fireEvent.click(screen.getByTestId('steer-edge-t1_corrected'));
    expect(screen.getByTestId('trajectory-drawer')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('trajectory-drawer-close'));
    expect(screen.queryByTestId('trajectory-drawer')).not.toBeInTheDocument();
    // Re-open, then dismiss by backdrop click.
    fireEvent.click(screen.getByTestId('steer-edge-t1_corrected'));
    expect(screen.getByTestId('trajectory-drawer')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('trajectory-drawer-backdrop'));
    expect(screen.queryByTestId('trajectory-drawer')).not.toBeInTheDocument();
  });

  it('drawer close via Escape key', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    fireEvent.click(screen.getByTestId('steer-edge-t1_corrected'));
    expect(screen.getByTestId('trajectory-drawer')).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('trajectory-drawer')).not.toBeInTheDocument();
  });

  it('legacy scrubber keyboard navigation works when the escape hatch is expanded', () => {
    setLegacyExpanded(true);
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    // With the legacy stacked sections opted in, the old RevisionScrubber
    // comes back along with its keyboard handlers.
    const scrubber = screen.getByTestId('revision-scrubber');
    fireEvent.click(screen.getByTestId('scrubber-notch-1'));
    // ArrowLeft → rev 0.
    fireEvent.keyDown(scrubber, { key: 'ArrowLeft' });
    expect(screen.getByTestId('scrubber-notch-0')).toHaveAttribute(
      'aria-selected',
      'true',
    );
    // End → Latest.
    fireEvent.keyDown(scrubber, { key: 'End' });
    expect(screen.getByTestId('scrubber-notch-latest')).toHaveAttribute(
      'aria-selected',
      'true',
    );
    // Home → rev 0.
    fireEvent.keyDown(scrubber, { key: 'Home' });
    expect(screen.getByTestId('scrubber-notch-0')).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('default layout hides the legacy stacked sections (ribbon-strip, intervention list, detail pane)', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    // The old Ribbon strip (trajectory-ribbon) is gone.
    expect(screen.queryByTestId('trajectory-ribbon')).not.toBeInTheDocument();
    // The INTERVENTIONS list header is gone.
    expect(screen.queryByTestId('trajectory-interventions')).not.toBeInTheDocument();
    // The RevisionScrubber is gone.
    expect(screen.queryByTestId('revision-scrubber')).not.toBeInTheDocument();
    // The REV N chip tablist is gone.
    expect(screen.queryByTestId('rev-chip-0')).not.toBeInTheDocument();
    expect(screen.queryByTestId('rev-chip-1')).not.toBeInTheDocument();
    // The legacy-stack wrapper is absent.
    expect(screen.queryByTestId('trajectory-legacy-stack')).not.toBeInTheDocument();
    // The new unified ribbon IS present.
    expect(screen.getByTestId('trajectory-timeline-ribbon')).toBeInTheDocument();
  });

  it('toggling the ribbon expand button brings back the legacy stacked sections', () => {
    seedTwoRevsWithReplacement();
    const { rerender } = render(<TrajectoryView />);
    // Expand toggle is present on the new ribbon.
    const toggle = screen.getByTestId('ribbon-expand-btn');
    expect(toggle).toBeInTheDocument();
    // Click toggles the uiStore pref; since tests mock the selector, flip
    // it directly then re-render to observe the opt-in state.
    setLegacyExpanded(true);
    rerender(<TrajectoryView />);
    expect(screen.getByTestId('trajectory-legacy-stack')).toBeInTheDocument();
    expect(screen.getByTestId('trajectory-ribbon')).toBeInTheDocument();
    expect(screen.getByTestId('revision-scrubber')).toBeInTheDocument();
  });

  it('clicking a task node opens the drawer with the TaskNodeDetail body', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const taskNode = screen.getByTestId('task-node-t1_corrected');
    fireEvent.click(taskNode);
    const drawer = screen.getByTestId('trajectory-drawer');
    expect(drawer).toBeInTheDocument();
    const detail = within(drawer).getByTestId('task-node-detail');
    expect(detail).toHaveTextContent('research paper');
    expect(detail).toHaveTextContent('pending');
  });

  it('does NOT draw a steering edge when the plan has only rev 0', () => {
    const rev0 = mkPlan('p1', [mkTask('t1', 'PENDING', 'agent-a')]);
    mockStore.tasks.upsertPlan(rev0);
    render(<TrajectoryView />);
    expect(screen.queryByTestId('trajectory-steer-edges')).not.toBeInTheDocument();
    expect(screen.queryByTestId('trajectory-supersedes-edges')).not.toBeInTheDocument();
  });
});
