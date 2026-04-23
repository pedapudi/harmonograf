/**
 * Regression test for the "Trajectory view is empty when a session has
 * a plan + task events + a drift storm" bug.
 *
 * Repro snapshot: a live session with a single rev-0 plan, no refines yet,
 * real task_started/task_completed updates, and *tens of thousands* of
 * UNSPECIFIED-kind drifts (a known goldfive status-query bug emits these
 * spuriously). Before the fix, the TrajectoryView Ribbon rendered a
 * <button> for every drift — 50k buttons under `flex-wrap: wrap` paved
 * the viewport with a giant grid of dots and pushed the DAG off-screen,
 * so the operator saw nothing useful.
 *
 * The fix has three parts tested here:
 *   1. UNSPECIFIED-kind drifts (kind === '') are filtered out up-front so
 *      they never reach the ribbon.
 *   2. Legitimate drifts are capped per rev with a "+N more" chip so a
 *      dense legitimate-drift window can't break layout either.
 *   3. The DAG, task-delta list, and plan header still render normally
 *      against real task data even with no refines yet (vm.revs.length >
 *      0 from rev 0 alone).
 */

import { render, screen } from '@testing-library/react';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Task, TaskPlan } from '../../gantt/types';

vi.mock('../../components/shell/views/views.css', () => ({}));

let mockStore = new SessionStore();
const mockSessionId: string | null = 'sess-traj';

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
  currentSessionId: mockSessionId,
  selectSpan: vi.fn(),
};
vi.mock('../../state/uiStore', () => ({
  useUiStore: <T,>(selector: (s: typeof uiStoreState) => T) =>
    selector(uiStoreState),
}));

// Minimal annotation store stub — no annotations for this session so the
// intervention list naturally collapses to empty. Subscribe is a no-op.
vi.mock('../../state/annotationStore', () => ({
  useAnnotationStore: Object.assign(
    () => ({ list: () => [] }),
    {
      getState: () => ({ list: () => [] }),
      subscribe: () => () => {},
    },
  ),
}));

import { TrajectoryView } from '../../components/shell/views/TrajectoryView';

function mkTask(id: string, status: Task['status'], title = id): Task {
  return {
    id,
    title,
    description: `desc for ${id}`,
    assigneeAgentId: 'agent-a',
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    cancelReason: '',
  };
}

function mkPlan(tasks: Task[]): TaskPlan {
  return {
    id: 'plan-0',
    invocationSpanId: 'inv-0',
    plannerAgentId: 'planner-agent',
    createdAtMs: 0,
    summary: 'Two-slide solar panel presentation',
    tasks,
    edges: [
      { fromTaskId: 't1', toTaskId: 't2' },
      { fromTaskId: 't2', toTaskId: 't3' },
    ],
    revisionReason: '',
    revisionIndex: 0,
  };
}

beforeEach(() => {
  mockStore = new SessionStore();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('<TrajectoryView /> with only rev 0 + drift storm', () => {
  it('renders the plan, DAG nodes, and task-delta for a session with no refines', () => {
    // Seed a rev-0 plan with 3 tasks in realistic statuses.
    const plan = mkPlan([
      mkTask('t1', 'COMPLETED', 'Research'),
      mkTask('t2', 'RUNNING', 'Draft'),
      mkTask('t3', 'CANCELLED', 'Verify'),
    ]);
    // t3 needs a cancelReason for the delta section to surface it.
    plan.tasks[2].cancelReason = 'superseded_by_revision';
    mockStore.tasks.upsertPlan(plan);

    render(<TrajectoryView />);

    // Header + rev chip show.
    expect(screen.getByText('Trajectory')).toBeInTheDocument();

    // Ribbon rendered.
    expect(screen.getByTestId('trajectory-ribbon')).toBeInTheDocument();
    expect(screen.getByTestId('rev-segment-0')).toBeInTheDocument();
    // Summary text from the plan surfaces.
    expect(
      screen.getByText(/Two-slide solar panel presentation/),
    ).toBeInTheDocument();

    // DAG renders each task as a node with its stable testid.
    expect(screen.getByTestId('trajectory-dag')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-t1')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-t2')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-t3')).toBeInTheDocument();

    // Task-delta lists the one CANCELLED task with its reason.
    expect(screen.getByTestId('trajectory-task-delta')).toBeInTheDocument();
    const cancelRow = screen.getByTestId('task-delta-row-t3');
    expect(cancelRow).toHaveTextContent('superseded_by_revision');
  });

  it('drops UNSPECIFIED-kind drifts from the ribbon entirely', () => {
    const plan = mkPlan([mkTask('t1', 'RUNNING')]);
    mockStore.tasks.upsertPlan(plan);

    // Simulate the status-query storm: 200 drifts with no kind at all.
    // The real reproducer has 50k, 200 is enough to prove the filter
    // kicks in without slowing the test.
    for (let i = 0; i < 200; i++) {
      mockStore.drifts.append({
        kind: '',
        severity: '',
        detail: `status_query ${i}`,
        taskId: 't1',
        agentId: 'agent-a',
        recordedAtMs: i,
        annotationId: '',
        driftId: `noise-${i}`,
      });
    }

    render(<TrajectoryView />);

    // No drift markers at all (every drift was unspec). No "+N more" chip
    // either, since the filter drops these before the cap applies.
    expect(screen.queryAllByTestId(/^drift-marker-/)).toHaveLength(0);
    expect(screen.queryByTestId('drift-more-0')).not.toBeInTheDocument();

    // Task node still renders — the ribbon never blocked it.
    expect(screen.getByTestId('task-node-t1')).toBeInTheDocument();
  });

  it('caps legitimate drifts per rev and shows a +N summary chip', () => {
    const plan = mkPlan([mkTask('t1', 'RUNNING')]);
    mockStore.tasks.upsertPlan(plan);

    // 60 real drifts (well above the 24 cap). Mix of severities so the
    // ranker has something to work on.
    const severities = ['info', 'warning', 'critical'] as const;
    for (let i = 0; i < 60; i++) {
      mockStore.drifts.append({
        kind: 'looping_reasoning',
        severity: severities[i % 3],
        detail: `d${i}`,
        taskId: 't1',
        agentId: 'agent-a',
        recordedAtMs: i,
        annotationId: '',
        driftId: `drift-${i}`,
      });
    }

    render(<TrajectoryView />);

    const markers = screen.queryAllByTestId(/^drift-marker-/);
    // Capped at RIBBON_MAX_MARKERS_PER_REV (24).
    expect(markers.length).toBeLessThanOrEqual(24);
    // Summary chip surfaces the trimmed remainder (60 - 24 = 36).
    const more = screen.getByTestId('drift-more-0');
    expect(more).toHaveTextContent('+36');
  });

  it('keeps the DAG visible even when the session has a mixed drift storm', () => {
    // This is the exact repro of the live bug: rev-0 plan, real task
    // statuses, a handful of legit drifts, and a firehose of UNSPECIFIED
    // noise. The DAG + task markers must still render.
    const plan = mkPlan([
      mkTask('t1', 'COMPLETED'),
      mkTask('t2', 'RUNNING'),
      mkTask('t3', 'PENDING'),
    ]);
    mockStore.tasks.upsertPlan(plan);

    // 2 legitimate drifts.
    mockStore.drifts.append({
      kind: 'looping_reasoning',
      severity: 'warning',
      detail: 'legit',
      taskId: 't2',
      agentId: 'agent-a',
      recordedAtMs: 1,
      annotationId: '',
      driftId: 'legit-1',
    });
    mockStore.drifts.append({
      kind: 'user_steer',
      severity: 'info',
      detail: 'operator nudge',
      taskId: 't2',
      agentId: 'agent-a',
      recordedAtMs: 2,
      annotationId: 'ann-1',
      driftId: 'legit-2',
    });
    // 500 UNSPECIFIED drifts from the status-query bug.
    for (let i = 0; i < 500; i++) {
      mockStore.drifts.append({
        kind: '',
        severity: '',
        detail: 'noise',
        taskId: '',
        agentId: 'agent-a',
        recordedAtMs: 3 + i,
        annotationId: '',
        driftId: `n-${i}`,
      });
    }

    render(<TrajectoryView />);

    // DAG + every task still there.
    expect(screen.getByTestId('task-node-t1')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-t2')).toBeInTheDocument();
    expect(screen.getByTestId('task-node-t3')).toBeInTheDocument();

    // Exactly the two legitimate drift markers, no noise.
    const markers = screen.queryAllByTestId(/^drift-marker-/);
    expect(markers).toHaveLength(2);
  });
});
