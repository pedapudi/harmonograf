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
import type { Task, TaskPlan } from '../../gantt/types';

let mockStore: SessionStore | undefined = undefined;
const mockSessionId: string | null = 'session-1';

vi.mock('../../rpc/hooks', () => ({
  getSessionStore: (id: string | null) => (id ? mockStore : undefined),
}));
vi.mock('../../state/uiStore', () => ({
  useUiStore: <T,>(selector: (s: { currentSessionId: string | null }) => T) =>
    selector({ currentSessionId: mockSessionId }),
}));

import { PlanRevisionBanner } from '../../components/shell/PlanRevisionBanner';

function mkTask(id: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    title: `task ${id}`,
    description: '',
    assigneeAgentId: 'a',
    status: 'PENDING',
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    ...overrides,
  };
}

function mkPlan(id: string, overrides: Partial<TaskPlan> = {}): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}`,
    plannerAgentId: 'planner',
    createdAtMs: 0,
    summary: '',
    tasks: [mkTask('t1'), mkTask('t2')],
    edges: [],
    revisionReason: '',
    ...overrides,
  };
}

describe('<PlanRevisionBanner /> diff counts', () => {
  beforeEach(() => {
    mockStore = new SessionStore();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    mockStore = undefined;
  });

  it('renders +added -removed ~modified counts on the pill', () => {
    // Seed p1 with 2 tasks, no revision yet.
    mockStore!.tasks.upsertPlan(
      mkPlan('p1', { tasks: [mkTask('a'), mkTask('b')] }),
    );
    render(<PlanRevisionBanner />);

    // Refine p1: remove b, add c, mutate a's status.
    act(() => {
      mockStore!.tasks.upsertPlan(
        mkPlan('p1', {
          revisionReason: 'replan',
          tasks: [mkTask('a', { status: 'RUNNING' }), mkTask('c')],
        }),
      );
    });

    const pill = screen.getByTestId('plan-revision-pill');
    const counts = screen.getByTestId('plan-revision-pill-counts');
    expect(pill).toHaveTextContent('replan');
    // +1 added (c), -1 removed (b), ~1 modified (a status)
    expect(counts).toHaveTextContent('+1 -1 ~1');
  });

  it('shows zeros when the revision reason changes but the task set does not', () => {
    mockStore!.tasks.upsertPlan(
      mkPlan('p1', {
        tasks: [mkTask('t1'), mkTask('t2')],
        revisionReason: 'first',
      }),
    );
    render(<PlanRevisionBanner />);
    act(() => {
      mockStore!.tasks.upsertPlan(
        mkPlan('p1', {
          tasks: [mkTask('t1'), mkTask('t2')],
          revisionReason: 'rebranded',
        }),
      );
    });
    const counts = screen.getByTestId('plan-revision-pill-counts');
    expect(counts).toHaveTextContent('+0 -0 ~0');
  });
});
