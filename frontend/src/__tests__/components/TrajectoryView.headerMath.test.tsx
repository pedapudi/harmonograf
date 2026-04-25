/**
 * Regression test for the Trajectory view header's "rev N of M" math.
 *
 * Bug: the header read `rev <arrayIdx> of <length-1>` instead of using
 * the actual `revisionIndex` field on each TaskPlan. For a session with
 * a gap in revision numbering (e.g. R0 + R2 with R1 missing because the
 * planner minted a fresh plan_id mid-stream), the header rendered
 * "rev 1 of 1" while the scrubber correctly labeled the notch "REV 2".
 * The two counts disagreed on what "revision" meant.
 *
 * Fix: header math now reads `currentRev.revisionIndex` for the
 * numerator and the highest `revisionIndex` across `vm.revs` for the
 * denominator. Empty plan history renders the placeholder
 * "no plan yet" instead of a cryptic "rev 0 of 0".
 */

import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { Task, TaskPlan } from '../../gantt/types';

vi.mock('../../components/shell/views/views.css', () => ({}));

let mockStore = new SessionStore();
const mockSessionId: string = 'sess-header-math';

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
    uiStoreState.trajectoryLegacyExpanded = !uiStoreState.trajectoryLegacyExpanded;
  },
  selectedRevision: null as number | null,
  setSelectedRevision: (rev: number | null): void => {
    uiStoreState.selectedRevision = rev;
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
  revisionIndex = 0,
  revisionReason = '',
  revisionKind = '',
): TaskPlan {
  return {
    id,
    invocationSpanId: `inv-${id}-${revisionIndex}`,
    plannerAgentId: 'planner-agent',
    // createdAtMs sorts revs in buildViewModel; match revisionIndex order.
    createdAtMs: revisionIndex,
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
});
afterEach(() => {
  vi.clearAllMocks();
});

describe('<TrajectoryView /> header "rev N of M" math', () => {
  it('renders "no plan yet" when the plan history is empty', () => {
    render(<TrajectoryView />);
    expect(headerHint()).toBe('no plan yet');
    // Defensive: never the cryptic "rev 0 of 0" for an empty session.
    expect(headerHint()).not.toContain('rev 0 of 0');
  });

  it('uses revisionIndex (not array position) for the single-rev case', () => {
    // Lone explicit revision with revisionIndex = 0 (the implicit initial).
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 0));
    render(<TrajectoryView />);
    // One rev → "rev 0 of 0". The denominator is the highest revisionIndex.
    expect(headerHint()).toBe('rev 0 of 0');
  });

  it('reads the highest revisionIndex for the denominator across a contiguous chain', () => {
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 0));
    mockStore.tasks.upsertPlan(
      mkPlan('p1', [mkTask('t1'), mkTask('t2')], 1, 'add followup', 'user_steer'),
    );
    mockStore.tasks.upsertPlan(
      mkPlan('p1', [mkTask('t1'), mkTask('t2'), mkTask('t3')], 2, 'next', 'goldfive'),
    );
    render(<TrajectoryView />);
    // Latest live → numerator pinned to highest revisionIndex.
    expect(headerHint()).toBe('rev 2 of 2');
  });

  it('uses revisionIndex (not length-1) when the rev numbering has a gap (R0 + R2, no R1)', () => {
    // Goldfive planners can mint a fresh plan_id mid-stream, leaving the
    // rev sequence non-contiguous when the trajectory merges both plans.
    // The header denominator must reflect the highest revisionIndex
    // observed, not `vm.revs.length - 1`.
    //
    // Multi-plan note (Item 4 of UX cleanup batch): two distinct plan
    // ids (p1, p2) means the header now prefixes the rev label with
    // "Plan {N} · " so operators can see which plan the current rev
    // belongs to (the H1 plan picker lands the picker UI on top of this
    // label). The numerator/denominator math is unchanged.
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 0));
    mockStore.tasks.upsertPlan(
      mkPlan('p2', [mkTask('t1'), mkTask('t2')], 2, 'user steer', 'user_steer'),
    );
    render(<TrajectoryView />);
    // vm.revs.length === 2, but max revisionIndex === 2 → "rev 2 of 2",
    // not the buggy "rev 1 of 1". Multi-plan prefix surfaces "Plan 2"
    // because the latest rev belongs to the second distinct plan id.
    expect(headerHint()).toBe('Plan 2 · rev 2 of 2');
  });

  it('switches between "no plan yet" and a "rev N of M" hint as plans land', () => {
    // Sanity: an empty session shows the placeholder; appending a plan
    // flips the hint to the indices-based form. The heading itself is
    // unaffected by either state.
    const { rerender } = render(<TrajectoryView />);
    expect(headerHint()).toBe('no plan yet');
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 0));
    rerender(<TrajectoryView />);
    expect(headerHint()).toMatch(/^rev \d+ of \d+$/);
    expect(
      screen.getByRole('heading', { name: 'Trajectory' }),
    ).toBeInTheDocument();
  });

  it('omits the multi-plan prefix in the single-plan case (Item 4)', () => {
    // One plan id across multiple revs → no "Plan N · " prefix; the
    // header reads exactly "rev N of M" as before.
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 0));
    mockStore.tasks.upsertPlan(
      mkPlan('p1', [mkTask('t1'), mkTask('t2')], 1, 'next', 'goldfive'),
    );
    render(<TrajectoryView />);
    expect(headerHint()).toBe('rev 1 of 1');
    expect(headerHint()).not.toContain('Plan');
  });

  it('prefixes "Plan N · " when multiple plan ids are present (Item 4)', () => {
    // Three distinct plan ids → header surfaces the index of the plan
    // the current rev belongs to, so an operator viewing rev 2 can tell
    // which plan they're scoped to ahead of the H1 plan picker landing.
    mockStore.tasks.upsertPlan(mkPlan('p1', [mkTask('t1')], 0));
    mockStore.tasks.upsertPlan(
      mkPlan('p2', [mkTask('t1'), mkTask('t2')], 1, 'split', 'plan_divergence'),
    );
    mockStore.tasks.upsertPlan(
      mkPlan('p3', [mkTask('t1'), mkTask('t2'), mkTask('t3')], 2, 'next', 'goldfive'),
    );
    render(<TrajectoryView />);
    // Latest rev belongs to the third distinct plan id → "Plan 3".
    expect(headerHint()).toBe('Plan 3 · rev 2 of 2');
  });
});
