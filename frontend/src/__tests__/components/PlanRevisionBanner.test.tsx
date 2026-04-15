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

function mkTask(id: string): Task {
  return {
    id,
    title: id,
    description: '',
    assigneeAgentId: 'a',
    status: 'PENDING',
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
  };
}

function mkPlan(id: string, reason = ''): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}`,
    plannerAgentId: 'planner',
    createdAtMs: 0,
    summary: '',
    tasks: [mkTask('t1')],
    edges: [],
    revisionReason: reason,
  };
}

describe('<PlanRevisionBanner />', () => {
  beforeEach(() => {
    mockStore = new SessionStore();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    mockStore = undefined;
  });

  it('renders nothing when there are no revisions', () => {
    const { container } = render(<PlanRevisionBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('does not flash a pill for revisions that already exist on mount', () => {
    mockStore!.tasks.upsertPlan(mkPlan('p1', 'preexisting'));
    const { container } = render(<PlanRevisionBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('shows a pill when a plan gains a new revisionReason', () => {
    mockStore!.tasks.upsertPlan(mkPlan('p1'));
    render(<PlanRevisionBanner />);
    act(() => {
      mockStore!.tasks.upsertPlan(mkPlan('p1', 'scope changed'));
    });
    const pills = screen.getAllByTestId('plan-revision-pill');
    expect(pills).toHaveLength(1);
    expect(pills[0]).toHaveTextContent('scope changed');
  });

  it('stacks up to 3 pills when revisions arrive rapidly', () => {
    mockStore!.tasks.upsertPlan(mkPlan('p1'));
    mockStore!.tasks.upsertPlan(mkPlan('p2'));
    mockStore!.tasks.upsertPlan(mkPlan('p3'));
    mockStore!.tasks.upsertPlan(mkPlan('p4'));
    render(<PlanRevisionBanner />);
    act(() => {
      mockStore!.tasks.upsertPlan(mkPlan('p1', 'r1'));
      mockStore!.tasks.upsertPlan(mkPlan('p2', 'r2'));
      mockStore!.tasks.upsertPlan(mkPlan('p3', 'r3'));
      mockStore!.tasks.upsertPlan(mkPlan('p4', 'r4'));
    });
    const pills = screen.getAllByTestId('plan-revision-pill');
    expect(pills).toHaveLength(3);
  });

  it('auto-dismisses pills after 4 seconds', () => {
    mockStore!.tasks.upsertPlan(mkPlan('p1'));
    const { container } = render(<PlanRevisionBanner />);
    act(() => {
      mockStore!.tasks.upsertPlan(mkPlan('p1', 'revised'));
    });
    expect(screen.getAllByTestId('plan-revision-pill')).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(container.firstChild).toBeNull();
  });
});
