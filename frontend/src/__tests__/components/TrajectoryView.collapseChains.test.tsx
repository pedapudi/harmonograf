/**
 * Acceptance test for the 5-revision supersedes-collapse integration.
 *
 * NOTE: the brief filename mentions TrajectoryView, but TrajectoryView's
 * DAG renderer (DagPane, custom SVG) is a separate code path that does
 * not call into the new collapsedLayout — the `TaskStagesGraph` /
 * `TaskPlanPanel` renderer is where SUPE+LAY+BAG land. We test that
 * path here, since it's the one the integration rewires. A follow-up
 * can extend the collapse to DagPane (see PR body / TODO below).
 */

import { fireEvent, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Task, TaskEdge, TaskPlan, TaskStatus } from '../../gantt/types';
import { SessionStore } from '../../gantt/index';

const storesById = new Map<string, SessionStore>();
vi.mock('../../rpc/hooks', () => ({
  getSessionStore: (id: string | null) => (id ? storesById.get(id) : undefined),
}));
vi.mock('../../components/TaskStages/TaskStagesGraph.css', () => ({}));
vi.mock('../../components/TaskStages/RevisionHistoryBadge.css', () => ({}));

import { TaskStagesGraph } from '../../components/TaskStages/TaskStagesGraph';
import { __internal } from '../../state/planHistory';

function mkTask(
  id: string,
  title: string,
  status: TaskStatus = 'PENDING',
  supersedes: string = '',
): Task {
  return {
    id,
    title,
    description: '',
    assigneeAgentId: 'agent-a',
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    supersedes,
  };
}

function mkPlan(
  rev: number,
  tasks: Task[],
  edges: Array<[string, string]>,
  opts: Partial<TaskPlan> = {},
): TaskPlan {
  return {
    id: 'plan-5',
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: rev * 1000,
    summary: '',
    tasks,
    edges: edges.map<TaskEdge>(([f, t]) => ({ fromTaskId: f, toTaskId: t })),
    revisionReason: opts.revisionReason ?? `rev ${rev}`,
    revisionKind: opts.revisionKind,
    revisionIndex: rev,
    triggerEventId: opts.triggerEventId,
  };
}

beforeEach(() => {
  storesById.clear();
});

/**
 * 5-revision fixture:
 *   R0: A, B, C. Edges A→B, B→C.
 *   R1: A' supersedes A (slot A chain: A→A').
 *   R2: A'' supersedes A'  AND  B' supersedes B.
 *   R3: A''' supersedes A''.
 *   R4: A'''' supersedes A'''.
 *
 *   After collapse we expect 3 chains: {A, A', A'', A''', A''''},
 *   {B, B'}, {C}. Three cards in the rendered DAG.
 */
function seedFiveRevSession(): {
  cumulative: ReturnType<typeof __internal.deriveCumulative>;
  supersedes: ReturnType<typeof __internal.deriveSupersedes>;
} {
  const R0 = mkPlan(
    0,
    [mkTask('A0', 'Research topic'), mkTask('B0', 'Draft plan'), mkTask('C0', 'Ship')],
    [
      ['A0', 'B0'],
      ['B0', 'C0'],
    ],
  );

  const R1 = mkPlan(
    1,
    [
      mkTask('A1', 'Research topic v2', 'PENDING', 'A0'),
      mkTask('B0', 'Draft plan'),
      mkTask('C0', 'Ship'),
    ],
    [
      ['A1', 'B0'],
      ['B0', 'C0'],
    ],
    { revisionKind: 'off_topic' },
  );

  const R2 = mkPlan(
    2,
    [
      mkTask('A2', 'Research topic v3', 'PENDING', 'A1'),
      mkTask('B1', 'Draft plan v2', 'PENDING', 'B0'),
      mkTask('C0', 'Ship'),
    ],
    [
      ['A2', 'B1'],
      ['B1', 'C0'],
    ],
    { revisionKind: 'user_steer' },
  );

  const R3 = mkPlan(
    3,
    [
      mkTask('A3', 'Research topic v4', 'PENDING', 'A2'),
      mkTask('B1', 'Draft plan v2'),
      mkTask('C0', 'Ship'),
    ],
    [
      ['A3', 'B1'],
      ['B1', 'C0'],
    ],
  );

  const R4 = mkPlan(
    4,
    [
      mkTask('A4', 'Research topic v5', 'PENDING', 'A3'),
      mkTask('B1', 'Draft plan v2'),
      mkTask('C0', 'Ship'),
    ],
    [
      ['A4', 'B1'],
      ['B1', 'C0'],
    ],
  );

  const plans = [R0, R1, R2, R3, R4];
  const cumulative = __internal.deriveCumulative('plan-5', plans);
  // deriveSupersedes needs a driftsById map; we don't seed drifts so pass empty.
  const supersedes = __internal.deriveSupersedes(plans, new Map());
  return { cumulative, supersedes };
}

describe('<TaskStagesGraph /> acceptance — 5-revision supersedes chain collapse', () => {
  it('renders exactly one card per logical slot (3 cards, not 8+ stacked)', () => {
    const { cumulative, supersedes } = seedFiveRevSession();
    expect(cumulative).not.toBeNull();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [], [])}
        cumulative={cumulative}
        supersedesMap={supersedes}
      />,
    );
    const cards = container.querySelectorAll('g.hg-stages__card');
    // Three logical slots: A-chain, B-chain, C. Not 5+3+1=9 stacked.
    expect(cards.length).toBe(3);
  });

  it('A-chain card carries a RevisionHistoryBadge covering all 5 members', () => {
    const { cumulative, supersedes } = seedFiveRevSession();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [], [])}
        cumulative={cumulative}
        supersedesMap={supersedes}
      />,
    );
    // Canonical of the A chain is A4 (latest).
    const aBadgeSlot = container.querySelector(
      '[data-testid="chain-badge-for-A4"]',
    );
    expect(aBadgeSlot).toBeTruthy();
    // Chain size: 5 members — check the card's data attribute we added
    // to the card <g>. The badge itself renders "R0→R1→R2→R3→R4"
    // inside its label span.
    const aCard = container.querySelector(
      'g.hg-stages__card[data-chain-size="5"]',
    );
    expect(aCard).toBeTruthy();
  });

  it('B-chain card carries a badge with 2 members; C is a singleton (no badge)', () => {
    const { cumulative, supersedes } = seedFiveRevSession();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [], [])}
        cumulative={cumulative}
        supersedesMap={supersedes}
      />,
    );
    // Canonical of B chain is B1.
    expect(container.querySelector('[data-testid="chain-badge-for-B1"]')).toBeTruthy();
    // Canonical of C chain is C0, singleton → NO badge.
    expect(container.querySelector('[data-testid="chain-badge-for-C0"]')).toBeNull();
    // Exactly two chain badges overall (A and B chains).
    expect(
      container.querySelectorAll('[data-testid^="chain-badge-for-"]').length,
    ).toBe(2);
  });

  it('clicking a predecessor row in the expanded A badge fires onTaskClick with that predecessor id', () => {
    const { cumulative, supersedes } = seedFiveRevSession();
    const handleTaskClick = vi.fn();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [], [])}
        cumulative={cumulative}
        supersedesMap={supersedes}
        onTaskClick={handleTaskClick}
      />,
    );
    // Expand the A-chain badge.
    const aBadge = container.querySelector('[data-testid="chain-badge-for-A4"]');
    expect(aBadge).toBeTruthy();
    // The badge pill exposes a toggle button when members.length > 1.
    const toggle = aBadge!.querySelector('button');
    expect(toggle).toBeTruthy();
    fireEvent.click(toggle!);
    // Expanded trail renders role="list" with one item per PREDECESSOR
    // (canonical is excluded — it's the card itself). 5 members →
    // 4 predecessor rows (newest-first per component contract).
    const list = aBadge!.querySelector('[role="list"]');
    expect(list).toBeTruthy();
    const items = list!.querySelectorAll('[role="listitem"]');
    expect(items.length).toBe(4);
    // Click the oldest predecessor — with newest-first ordering that's
    // the LAST item, which corresponds to members[0] = A0 (R0).
    const lastItem = items[items.length - 1];
    expect(lastItem.getAttribute('data-task-id')).toBe('A0');
    const rowBtn = lastItem.querySelector('button');
    expect(rowBtn).toBeTruthy();
    fireEvent.click(rowBtn!);
    expect(handleTaskClick).toHaveBeenCalled();
    const clickedTask = handleTaskClick.mock.calls[0][0] as Task;
    expect(clickedTask.id).toBe('A0');
  });

  it('pinning to R2 via revisionFilter: A-chain and B-chain stay visible (born at R0); C stays visible', () => {
    const { cumulative, supersedes } = seedFiveRevSession();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [], [])}
        cumulative={cumulative}
        supersedesMap={supersedes}
        revisionFilter={2}
      />,
    );
    // All three chains are born at/before R2 (firstRev=0 for all of
    // them). LAY's `filterCollapsedAtRevision` muting trade-off: when a
    // chain's latest canonical is introduced > pinned rev the chain is
    // MUTED but still rendered. For the A chain, latest canonical A4
    // was introduced at R4 > 2 → muted. B chain canonical B1 introduced
    // at R2 → not muted. C canonical C0 introduced at R0 → not muted.
    const cards = container.querySelectorAll('g.hg-stages__card');
    expect(cards.length).toBe(3);
    const mutedCards = Array.from(cards).filter((c) =>
      c.classList.contains('hg-stages__card--muted'),
    );
    // A chain is muted (canonical = A4 @ R4, pinned R2); B + C are not.
    // This is the current LAY contract: muted, not canonical-swapped to
    // the R2-era member. A follow-up issue can tighten this to swap the
    // displayed canonical to the newest member ≤ R — tracked in the
    // collapsedLayout docstring.
    expect(mutedCards.length).toBe(1);
    expect(mutedCards[0].querySelector('title')?.textContent).toContain(
      'Research topic v5',
    );
  });
});
