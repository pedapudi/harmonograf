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
    // task isn't present in the latest rev. Verified by mousedown firing
    // without throwing: React event wiring doesn't drop superseded nodes.
    fireEvent.click(superseded);
    // After click the detail region reflects "Task not present in this rev"
    // because the latest-rev view doesn't carry the superseded task body;
    // the selection state still moved — the panel shows *something* from
    // the detail pane family.
    expect(
      screen.getByText(/Task not present in this rev|detail-task/i),
    ).toBeInTheDocument();
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

  it('revision scrubber hides tasks introduced after the pinned revision', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    // Before scrubbing: both tasks visible.
    expect(screen.getByTestId('task-node-t1')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-t1_corrected')).toBeInTheDocument();
    // Click the REV 0 scrubber notch.
    const notch = screen.getByTestId('scrubber-notch-0');
    fireEvent.click(notch);
    // After scrubbing to rev 0: rev-1 task is hidden.
    expect(screen.getByTestId('task-node-t1')).toBeInTheDocument();
    expect(screen.queryByTestId('task-node-t1_corrected')).not.toBeInTheDocument();
    // "Latest" returns the full cumulative view.
    fireEvent.click(screen.getByTestId('scrubber-notch-latest'));
    expect(screen.getByTestId('task-node-t1_corrected')).toBeInTheDocument();
  });

  it('clicking a steering arrow opens the detail panel with Trigger / Steering / Target', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const arrow = screen.getByTestId('steer-edge-t1_corrected');
    fireEvent.click(arrow);
    const panel = screen.getByTestId('steering-detail-panel');
    expect(panel).toBeInTheDocument();
    // Three sections: Trigger, Steering, Target.
    expect(within(panel).getByTestId('steering-detail-trigger')).toBeInTheDocument();
    expect(within(panel).getByTestId('steering-detail-steering')).toBeInTheDocument();
    const targetSec = within(panel).getByTestId('steering-detail-target');
    // Target section names the agent explicitly — the primary UX guarantee.
    expect(
      within(targetSec).getByTestId('steering-detail-target-agent'),
    ).toHaveTextContent('research_agent');
    expect(
      within(targetSec).getByTestId('steering-detail-target-task'),
    ).toHaveTextContent(/research paper/);
    // Kind + reason surface in the Trigger section.
    expect(
      within(panel).getByTestId('steering-detail-trigger'),
    ).toHaveTextContent('off_topic');
    expect(
      within(panel).getByTestId('steering-detail-trigger'),
    ).toHaveTextContent('coordinator looped on status queries');
  });

  it('clicking a supersedes edge opens the same detail panel with the old / new task pair', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const edge = screen.getByTestId('supersedes-edge-t1');
    // SVG-native click — fireEvent dispatches to the parent <g>.
    fireEvent.click(edge);
    const panel = screen.getByTestId('steering-detail-panel');
    expect(panel).toBeInTheDocument();
    expect(panel).toHaveTextContent('t1');
    expect(panel).toHaveTextContent('t1_corrected');
  });

  it('steering panel close button dismisses the panel and restores task-detail pane', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    fireEvent.click(screen.getByTestId('steer-edge-t1_corrected'));
    expect(screen.getByTestId('steering-detail-panel')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('steering-detail-close'));
    expect(screen.queryByTestId('steering-detail-panel')).not.toBeInTheDocument();
  });

  it('scrubber keyboard navigation steps through revisions and returns to Latest', () => {
    seedTwoRevsWithReplacement();
    render(<TrajectoryView />);
    const scrubber = screen.getByTestId('revision-scrubber');
    // Seed the scrubber with a pinned rev 1 first so we can verify left-arrow.
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

  it('does NOT draw a steering edge when the plan has only rev 0', () => {
    const rev0 = mkPlan('p1', [mkTask('t1', 'PENDING', 'agent-a')]);
    mockStore.tasks.upsertPlan(rev0);
    render(<TrajectoryView />);
    expect(screen.queryByTestId('trajectory-steer-edges')).not.toBeInTheDocument();
    expect(screen.queryByTestId('trajectory-supersedes-edges')).not.toBeInTheDocument();
  });
});
