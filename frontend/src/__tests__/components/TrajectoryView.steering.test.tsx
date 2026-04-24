/**
 * harmonograf#196: TrajectoryView surfaces goldfive's steering moves.
 *
 *   1. Steering arrow — a dashed goldfive-colored arrow draws from a
 *      small "g5" gutter node on the DAG's left edge to the target
 *      task node when a PlanRevised carries a target agent.
 *   2. User gutter — a "u" node renders when a RunStarted event
 *      produced a user-goal span; drawn only on rev 0 so later refine
 *      panes don't accumulate stale user arrows.
 *   3. Intervention detail panel — clicking a drift marker surfaces
 *      Trigger / Steering / Target sections (testid-guarded).
 */

import { render, screen, fireEvent } from '@testing-library/react';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Span, Task, TaskPlan } from '../../gantt/types';

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
): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}`,
    plannerAgentId: 'planner-agent',
    createdAtMs: 0,
    summary: `plan ${id}`,
    tasks,
    edges: [],
    revisionReason,
    revisionKind,
    revisionSeverity: 'warning',
    revisionIndex,
    triggerEventId: '',
  };
}

// Minimal synth-span builder mirroring goldfiveEvent.synthesizeRefineSpan.
function synthRefineSpan(
  revisionIndex: number,
  kind: string,
  reason: string,
  targetAgent: string,
  atMs: number,
): Span {
  return {
    id: `refine-${atMs}-${revisionIndex}`,
    sessionId: mockSessionId ?? '',
    agentId: '__goldfive__',
    parentSpanId: null,
    kind: 'CUSTOM',
    status: 'COMPLETED',
    name: `refine: ${kind}`,
    startMs: atMs,
    endMs: atMs,
    links: [],
    attributes: {
      'refine.index': { kind: 'string', value: String(revisionIndex) },
      'refine.kind': { kind: 'string', value: kind },
      'refine.severity': { kind: 'string', value: 'warning' },
      'refine.reason': { kind: 'string', value: reason },
      'refine.target_agent_id': { kind: 'string', value: targetAgent },
      'harmonograf.synthetic_span': { kind: 'bool', value: true },
    },
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
}

function synthUserGoalSpan(goal: string, atMs: number): Span {
  return {
    id: `user-goal-${atMs}`,
    sessionId: mockSessionId ?? '',
    agentId: '__user__',
    parentSpanId: null,
    kind: 'USER_MESSAGE',
    status: 'COMPLETED',
    name: goal,
    startMs: atMs,
    endMs: atMs,
    links: [],
    attributes: {
      'user.goal_summary': { kind: 'string', value: goal },
      'user.run_id': { kind: 'string', value: 'run-1' },
      'harmonograf.synthetic_span': { kind: 'bool', value: true },
    },
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
}

beforeEach(() => {
  mockStore = new SessionStore();
});
afterEach(() => {
  vi.clearAllMocks();
});

describe('<TrajectoryView /> steering arrow + user gutter + detail panel', () => {
  it('draws a steering edge from goldfive to the target task when PlanRevised has a target', () => {
    // rev 0
    const rev0 = mkPlan('p1', [mkTask('t1', 'COMPLETED', 'agent-a')]);
    mockStore.tasks.upsertPlan(rev0);
    // rev 1 — add t2 assigned to agent-b; synth the refine span with
    // target_agent_id = agent-b.
    const rev1 = mkPlan(
      'p1',
      [mkTask('t1', 'COMPLETED', 'agent-a'), mkTask('t2', 'PENDING', 'agent-b')],
      1,
      'add verification',
      'looping_reasoning',
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
    mockStore.spans.append(
      synthRefineSpan(1, 'looping_reasoning', 'add verification', 'agent-b', 5),
    );

    render(<TrajectoryView />);

    // Steering edge present, pointing at t2 (the task assigned to agent-b).
    expect(screen.getByTestId('trajectory-steer-edges')).toBeInTheDocument();
    expect(screen.getByTestId('steer-edge-t2')).toBeInTheDocument();
    // Goldfive gutter rendered.
    expect(
      screen.getByTestId('trajectory-goldfive-gutter'),
    ).toBeInTheDocument();
  });

  it('does NOT draw a steering edge on rev 0 (no steering yet)', () => {
    const rev0 = mkPlan('p1', [mkTask('t1', 'PENDING', 'agent-a')]);
    mockStore.tasks.upsertPlan(rev0);
    render(<TrajectoryView />);
    expect(
      screen.queryByTestId('trajectory-steer-edges'),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('trajectory-goldfive-gutter'),
    ).not.toBeInTheDocument();
  });

  it('renders the user gutter + user edge on rev 0 when a user-goal span is present', () => {
    const rev0 = mkPlan('p1', [mkTask('t1', 'PENDING', 'agent-a')]);
    mockStore.tasks.upsertPlan(rev0);
    mockStore.agents.upsert({
      id: '__user__',
      name: 'user',
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
    mockStore.spans.append(synthUserGoalSpan('summarize the paper', 0));

    render(<TrajectoryView />);
    expect(screen.getByTestId('trajectory-user-gutter')).toBeInTheDocument();
    expect(screen.getByTestId('trajectory-user-edge')).toBeInTheDocument();
  });

  it('intervention detail panel shows Trigger + Target when a drift is selected', () => {
    const rev0 = mkPlan('p1', [mkTask('t1', 'RUNNING', 'agent-a')]);
    mockStore.tasks.upsertPlan(rev0);
    mockStore.drifts.append({
      kind: 'looping_reasoning',
      severity: 'warning',
      detail: 'repeated tool calls',
      taskId: 't1',
      agentId: 'agent-a',
      recordedAtMs: 10,
      annotationId: '',
      driftId: 'drift-1',
    });

    render(<TrajectoryView />);

    // Click the drift marker — the only one on rev 0.
    const marker = screen.getByTestId(/^drift-marker-/);
    fireEvent.click(marker);

    expect(screen.getByTestId('detail-drift')).toBeInTheDocument();
    // Trigger section surfaces the drift detail.
    const trigger = screen.getByTestId('detail-drift-trigger');
    expect(trigger).toHaveTextContent('repeated tool calls');
    // Target section surfaces agent-a.
    const target = screen.getByTestId('detail-drift-target');
    expect(target).toHaveTextContent('agent-a');
  });

  it('intervention detail panel composes Steering from a matching PlanRevised', () => {
    // rev 0 + drift + rev 1 with triggerEventId = drift.driftId.
    const rev0 = mkPlan('p1', [mkTask('t1', 'RUNNING', 'agent-a')]);
    mockStore.tasks.upsertPlan(rev0);
    mockStore.drifts.append({
      kind: 'looping_reasoning',
      severity: 'warning',
      detail: 'loop',
      taskId: 't1',
      agentId: 'agent-a',
      recordedAtMs: 10,
      annotationId: '',
      driftId: 'drift-42',
    });
    const rev1 = mkPlan(
      'p1',
      [mkTask('t1', 'RUNNING', 'agent-a'), mkTask('t2', 'PENDING', 'agent-b')],
      1,
      'add verification',
      'looping_reasoning',
    );
    rev1.triggerEventId = 'drift-42';
    mockStore.tasks.upsertPlan(rev1);
    mockStore.spans.append(
      synthRefineSpan(1, 'looping_reasoning', 'add verification', 'agent-b', 11),
    );

    render(<TrajectoryView />);

    const marker = screen.getByTestId(/^drift-marker-/);
    fireEvent.click(marker);

    const steering = screen.getByTestId('detail-drift-steering');
    expect(steering).toHaveTextContent('add verification');
    const target = screen.getByTestId('detail-drift-target');
    // Refine's target_agent_id wins over drift's current_agent_id.
    expect(target).toHaveTextContent('agent-b');
  });
});
