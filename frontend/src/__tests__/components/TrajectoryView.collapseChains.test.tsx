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

  it('A-chain card carries a corner rev chip covering all 5 members', () => {
    const { cumulative, supersedes } = seedFiveRevSession();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [], [])}
        cumulative={cumulative}
        supersedesMap={supersedes}
      />,
    );
    // Canonical of the A chain is A4 (latest). The rev-chip overlay
    // anchors to the canonical's card.
    const aChip = container.querySelector(
      '[data-testid="rev-chip-for-A4"]',
    );
    expect(aChip).toBeTruthy();
    // The chip itself is a RevisionHistoryBadge; for a multi-member
    // chain its pill text is "R0→R1→R2→R3→R4".
    expect(aChip!.textContent).toContain('R0→R1→R2→R3→R4');
  });

  it('each canonical card carries exactly one corner rev chip (chains and singletons alike)', () => {
    const { cumulative, supersedes } = seedFiveRevSession();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [], [])}
        cumulative={cumulative}
        supersedesMap={supersedes}
      />,
    );
    // Canonicals: A4 (chain of 5), B1 (chain of 2), C0 (singleton).
    // All three render a single corner chip (one axis, one chip).
    expect(container.querySelector('[data-testid="rev-chip-for-A4"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="rev-chip-for-B1"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="rev-chip-for-C0"]')).toBeTruthy();
    expect(
      container.querySelectorAll('[data-testid^="rev-chip-for-"]').length,
    ).toBe(3);
    // Singleton chip renders the introduction rev as a muted label.
    const cChip = container.querySelector('[data-testid="rev-chip-for-C0"]');
    expect(cChip!.textContent).toContain('R0');
  });

  it('clicking a predecessor row in the expanded A chip fires onTaskClick with that predecessor id', () => {
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
    // Expand the A-chain chip.
    const aChip = container.querySelector('[data-testid="rev-chip-for-A4"]');
    expect(aChip).toBeTruthy();
    // The chip pill exposes a toggle button when members.length > 1.
    const toggle = aChip!.querySelector('button');
    expect(toggle).toBeTruthy();
    fireEvent.click(toggle!);
    // Expanded trail renders role="list" with one item per PREDECESSOR
    // (canonical is excluded — it's the card itself). 5 members →
    // 4 predecessor rows (newest-first per component contract).
    const list = aChip!.querySelector('[role="list"]');
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

  it('pinning to R2 via revisionFilter: future-rev canonicals are HIDDEN (not muted); B + C stay visible', () => {
    const { cumulative, supersedes } = seedFiveRevSession();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [], [])}
        cumulative={cumulative}
        supersedesMap={supersedes}
        revisionFilter={2}
      />,
    );
    // Plan-view redesign: chains whose canonical was introduced after
    // the pin are HIDDEN (not muted / ghost-dashed). A-chain canonical
    // A4 was introduced at R4, pin R2 → hidden entirely. B canonical B1
    // (R2) and C canonical C0 (R0) remain visible. 2 cards, not 3.
    const cards = container.querySelectorAll('g.hg-stages__card');
    expect(cards.length).toBe(2);
    // No `--muted` / `--superseded` class variants exist anymore; verify
    // no card carries either (legacy styles removed).
    const anyMuted = container.querySelector(
      'g.hg-stages__card--muted, g.hg-stages__card--superseded',
    );
    expect(anyMuted).toBeNull();
  });
});
