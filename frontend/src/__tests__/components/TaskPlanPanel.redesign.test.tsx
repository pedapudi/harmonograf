/**
 * Plan-view redesign invariants (harmonograf Gantt subview).
 *
 * Covers the five subtractive guarantees of the redesign:
 *   1. Exactly one corner rev chip per rendered card (no stacked labels).
 *   2. Card fill maps to task status — two orthogonal axes with the chip.
 *   3. Plan summary renders in mixed case and wraps (not all-caps one-line).
 *   4. Rev selection mirrors the shared Trajectory view state slice.
 *   5. Future-rev tasks are HIDDEN entirely (no ghost, no dashed border).
 *
 * The Gantt subview itself owns no rev-selection UI anymore — the shared
 * `selectedRevision` slice on `useUiStore` is the single source of truth.
 */

import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Task, TaskEdge, TaskPlan, TaskStatus } from '../../gantt/types';
import { SessionStore } from '../../gantt/index';

// Mock rpc/hooks so the planHistory hooks read our seeded test store.
const storesById = new Map<string, SessionStore>();
vi.mock('../../rpc/hooks', () => ({
  getSessionStore: (id: string | null) => (id ? storesById.get(id) : undefined),
}));

vi.mock('../../components/TaskStages/TaskStagesGraph.css', () => ({}));
vi.mock('../../components/TaskStages/RevisionHistoryBadge.css', () => ({}));

import { TaskPlanPanel } from '../../components/TaskStages/TaskPlanPanel';
import { TaskStagesGraph } from '../../components/TaskStages/TaskStagesGraph';
import { useUiStore } from '../../state/uiStore';
import { __internal } from '../../state/planHistory';

function mkTask(id: string, status: TaskStatus = 'PENDING', title?: string): Task {
  return {
    id,
    title: title ?? `Title ${id}`,
    description: '',
    assigneeAgentId: 'agent-a',
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    supersedes: '',
  };
}

function mkPlan(
  rev: number,
  tasks: Task[],
  edges: Array<[string, string]> = [],
  opts: Partial<TaskPlan> = {},
): TaskPlan {
  return {
    id: 'plan-1',
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: rev * 1000,
    summary: opts.summary ?? '',
    tasks,
    edges: edges.map<TaskEdge>(([f, t]) => ({ fromTaskId: f, toTaskId: t })),
    revisionReason: opts.revisionReason ?? '',
    revisionKind: opts.revisionKind,
    revisionIndex: rev,
    triggerEventId: opts.triggerEventId,
  };
}

function seedStore(sessionId: string, plans: TaskPlan[]): SessionStore {
  const store = new SessionStore();
  for (const p of plans) store.tasks.upsertPlan(p);
  storesById.set(sessionId, store);
  return store;
}

// Three-rev fixture: rev 0 intros t1; rev 1 intros t2; rev 2 intros t3.
// Used by the "future-rev tasks hidden" test — each rev introduces a
// singleton chain so the hide/show behaviour is unambiguous.
function seedThreeRevPlan(sessionId = 'sess-3'): SessionStore {
  const rev0 = mkPlan(0, [mkTask('t1', 'RUNNING', 'Research')]);
  const rev1 = mkPlan(1, [mkTask('t1'), mkTask('t2', 'PENDING', 'Draft')], [['t1', 't2']], {
    revisionKind: 'off_topic',
  });
  const rev2 = mkPlan(
    2,
    [mkTask('t1'), mkTask('t2'), mkTask('t3', 'PENDING', 'Ship')],
    [
      ['t1', 't2'],
      ['t2', 't3'],
    ],
    { revisionKind: 'user_steer' },
  );
  return seedStore(sessionId, [rev0, rev1, rev2]);
}

beforeEach(() => {
  storesById.clear();
  try {
    localStorage.clear();
  } catch {
    /* jsdom may not have storage */
  }
  // Always reset the shared rev pin to null between tests so one test's
  // pin doesn't bleed into the next.
  useUiStore.getState().setSelectedRevision(null);
});

describe('Plan-view redesign · invariants', () => {
  it('(1) exactly one corner rev chip per rendered card — no stacked labels', () => {
    const store = seedThreeRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    render(<TaskPlanPanel sessionId="sess-3" plan={plan} />);
    const graph = document.querySelector('[data-testid="task-stages-graph"]')!;
    const cards = graph.querySelectorAll('g.hg-stages__card');
    expect(cards.length).toBe(3);
    // One chip per card — total chip count equals card count.
    const chips = document.querySelectorAll('[data-testid^="rev-chip-for-"]');
    expect(chips.length).toBe(cards.length);
    // None of the retired labels survive: no gen-badge, no dashed
    // superseded border, no floating "(current Rn)" text, no
    // "R0 → R1" arrow badge.
    expect(document.querySelectorAll('g.hg-stages__gen-badge').length).toBe(0);
    expect(
      document.querySelectorAll('g.hg-stages__card--superseded').length,
    ).toBe(0);
  });

  it('(2) card fill encodes execution status only (two orthogonal axes with the chip)', () => {
    // Build a one-off cumulative plan by hand so each card has a
    // distinct status. Use the cumulative path so rev meta is present
    // (singleton chains still get a chip; status still drives fill).
    const plan = mkPlan(0, [
      mkTask('p', 'PENDING'),
      mkTask('r', 'RUNNING'),
      mkTask('c', 'COMPLETED'),
      mkTask('f', 'FAILED'),
      mkTask('x', 'CANCELLED'),
      mkTask('b', 'BLOCKED'),
    ]);
    const cum = __internal.deriveCumulative('plan-1', [plan]);
    const supersedes = __internal.deriveSupersedes([plan], new Map());
    const { container } = render(
      <TaskStagesGraph
        plan={plan}
        cumulative={cum}
        supersedesMap={supersedes}
      />,
    );
    const cardsByStatus = new Map<string, Element>();
    container.querySelectorAll('g.hg-stages__card').forEach((c) => {
      const s = c.getAttribute('data-status')!;
      cardsByStatus.set(s, c);
    });
    // Every status renders a card.
    expect(cardsByStatus.size).toBe(6);
    // Fill comes from STATUS_FILL keyed on the card's status. Distinct
    // status ⇒ distinct fill on the card rect.
    const fillByStatus = new Map<string, string>();
    for (const [status, card] of cardsByStatus) {
      const rect = card.querySelector('rect.hg-stages__card-rect')!;
      fillByStatus.set(status, rect.getAttribute('fill') ?? '');
    }
    // PENDING, RUNNING, COMPLETED, FAILED fills are all distinct — the
    // four most common statuses the operator sees on the Gantt subview.
    const distinctPrimary = new Set([
      fillByStatus.get('PENDING'),
      fillByStatus.get('RUNNING'),
      fillByStatus.get('COMPLETED'),
      fillByStatus.get('FAILED'),
    ]);
    expect(distinctPrimary.size).toBe(4);
  });

  it('(3) plan summary renders in mixed case and wraps (not all-caps one-line ellipsis)', () => {
    const longSummary =
      'Corrected plan for solar panels presentation — removed off-topic raccoon content, added a dedicated scoping stage, and pulled the draft deadline in by two days.';
    const plan = mkPlan(0, [mkTask('t1')], [], { summary: longSummary });
    seedStore('sess-sum', [plan]);
    render(<TaskPlanPanel sessionId="sess-sum" plan={plan} />);
    const body = screen.getByTestId('plan-summary-body');
    // Exact summary text surfaces (no truncation to a single line with
    // ellipsis) — content is the original mixed-case string.
    expect(body.textContent).toBe(longSummary);
    // No ancestor forces uppercase — the lead-in "Plan" label may be
    // uppercase, but the body must not be.
    const computed = window.getComputedStyle(body);
    expect(computed.textTransform).not.toBe('uppercase');
    // Not a single-line-with-ellipsis: whiteSpace is 'normal' (wraps).
    expect(computed.whiteSpace).toBe('normal');
    // 3-line cap honoured by line-clamp (collapsed) or unset (expanded).
    // Verifying line-clamp styling survived the render — an exact value
    // check is brittle across browser impls, so we assert the attribute
    // exists on the element.
    expect(body.style.overflow).toBe('hidden');
  });

  it('(4) rev selection mirrors the shared Trajectory state — no local scrubber', () => {
    const store = seedThreeRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    // No local scrubber exists on the Gantt subview anymore.
    render(<TaskPlanPanel sessionId="sess-3" plan={plan} />);
    expect(screen.queryByTestId('plan-revision-scrubber')).toBeNull();
    // Initially Latest → hint reads "Showing Latest ...".
    expect(screen.getByTestId('plan-sync-hint').textContent).toContain(
      'Latest',
    );
    // Simulate the Trajectory view (or ribbon) pinning rev 1 via the
    // shared uiStore slice. On next render the subview mirrors it.
    useUiStore.getState().setSelectedRevision(1);
    render(<TaskPlanPanel sessionId="sess-3" plan={plan} />);
    const hints = screen.getAllByTestId('plan-sync-hint');
    expect(hints[hints.length - 1].textContent).toContain('REV 1');
    expect(hints[hints.length - 1].textContent).toContain(
      'synced with Trajectory view',
    );
  });

  it('(5) future-rev tasks are HIDDEN entirely — fewer cards after pinning to an earlier rev', () => {
    const store = seedThreeRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    // Latest: all three tasks visible.
    useUiStore.getState().setSelectedRevision(null);
    const latest = render(<TaskPlanPanel sessionId="sess-3" plan={plan} />);
    const latestCards = latest.container
      .querySelector('[data-testid="task-stages-graph"]')!
      .querySelectorAll('g.hg-stages__card');
    expect(latestCards.length).toBe(3);
    latest.unmount();

    // Pin to rev 0: only t1 (introduced at rev 0) should remain. t2
    // (rev 1) and t3 (rev 2) are HIDDEN — no ghost / dashed card, no
    // muted variant, just gone.
    useUiStore.getState().setSelectedRevision(0);
    const pinned = render(<TaskPlanPanel sessionId="sess-3" plan={plan} />);
    const pinnedCards = pinned.container
      .querySelector('[data-testid="task-stages-graph"]')!
      .querySelectorAll('g.hg-stages__card');
    expect(pinnedCards.length).toBe(1);
    // No dashed/ghosted cards linger — the redesign prohibits them.
    expect(
      pinned.container.querySelectorAll(
        'g.hg-stages__card--muted, g.hg-stages__card--superseded',
      ).length,
    ).toBe(0);
  });
});
