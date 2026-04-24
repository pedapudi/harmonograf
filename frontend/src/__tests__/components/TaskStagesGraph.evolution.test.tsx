import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { vi } from 'vitest';
import type { Task, TaskEdge, TaskPlan, TaskStatus } from '../../gantt/types';
import { SessionStore } from '../../gantt/index';

// Mock the rpc/hooks module BEFORE importing the component so that the
// planHistory hooks pick up our test store. getSessionStore is the only
// thing the hooks reach into.
const storesById = new Map<string, SessionStore>();
vi.mock('../../rpc/hooks', () => ({
  getSessionStore: (id: string | null) => (id ? storesById.get(id) : undefined),
}));

// Mock CSS so vitest's "css: false" doesn't complain about side-effect
// imports on the component under test.
vi.mock('../../components/TaskStages/TaskStagesGraph.css', () => ({}));
vi.mock('../../components/TaskStages/RevisionHistoryBadge.css', () => ({}));

// Imported AFTER the mock so the hooks bind to our fake getSessionStore.
import { TaskStagesGraph } from '../../components/TaskStages/TaskStagesGraph';
import { TaskPlanPanel } from '../../components/TaskStages/TaskPlanPanel';
import {
  __internal,
  type CumulativePlan,
  type SupersessionLink,
} from '../../state/planHistory';
import { useUiStore } from '../../state/uiStore';

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
    summary: '',
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

beforeEach(() => {
  storesById.clear();
  try {
    localStorage.clear();
  } catch {
    /* jsdom may not have storage */
  }
});

// Two revisions: rev 0 introduces t1+t2; rev 1 supersedes t2 with t3 +
// a drift stamped by goldfive with an off_topic kind.
function seedTwoRevPlan(): SessionStore {
  const rev0 = mkPlan(0, [mkTask('t1', 'COMPLETED'), mkTask('t2', 'RUNNING')], [['t1', 't2']]);
  const rev1 = mkPlan(
    1,
    [mkTask('t1', 'COMPLETED'), mkTask('t3', 'PENDING', 'Refined task')],
    [['t1', 't3']],
    {
      revisionReason: 'Agent drifted off topic; focus on scoping.',
      revisionKind: 'off_topic',
      triggerEventId: 'drift-abc',
    },
  );
  const store = seedStore('sess-1', [rev0, rev1]);
  store.drifts.append({
    kind: 'off_topic',
    severity: 'warning',
    detail: 'Started writing unrelated code',
    taskId: 't2',
    agentId: 'agent-a',
    recordedAtMs: 500,
    annotationId: '',
    driftId: 'drift-abc',
    authoredBy: 'goldfive',
  });
  return store;
}

// Three revisions for the scrubber test: rev 0 introduces t1, rev 1
// introduces t2, rev 2 introduces t3.
function seedThreeRevPlan(): SessionStore {
  const rev0 = mkPlan(0, [mkTask('t1')]);
  const rev1 = mkPlan(1, [mkTask('t1'), mkTask('t2')], [['t1', 't2']], {
    revisionKind: 'off_topic',
    revisionReason: 'rev1',
    triggerEventId: 'drift-1',
  });
  const rev2 = mkPlan(
    2,
    [mkTask('t1'), mkTask('t2'), mkTask('t3')],
    [
      ['t1', 't2'],
      ['t2', 't3'],
    ],
    { revisionKind: 'user_steer', revisionReason: 'rev2', triggerEventId: 'ann-2' },
  );
  return seedStore('sess-3', [rev0, rev1, rev2]);
}

describe('plan-evolution · pure derivation', () => {
  it('deriveCumulative unions tasks across revisions with introduced/superseded meta', () => {
    const rev0 = mkPlan(0, [mkTask('t1'), mkTask('t2')]);
    const rev1 = mkPlan(1, [mkTask('t1'), mkTask('t3')], [], {
      revisionKind: 'off_topic',
    });
    const cum = __internal.deriveCumulative('p', [rev0, rev1]) as CumulativePlan;
    expect(cum).not.toBeNull();
    const ids = cum.tasks.map((t) => t.id).sort();
    expect(ids).toEqual(['t1', 't2', 't3']);
    expect(cum.taskRevisionMeta.get('t1')?.isSuperseded).toBe(false);
    expect(cum.taskRevisionMeta.get('t2')?.isSuperseded).toBe(true);
    expect(cum.taskRevisionMeta.get('t3')?.introducedInRevision).toBe(1);
  });

  it('deriveSupersedes pairs retired t2 with added t3 using title + assignee affinity', () => {
    const rev0 = mkPlan(0, [mkTask('t1'), mkTask('t2', 'PENDING', 'scope the work')]);
    const rev1 = mkPlan(
      1,
      [mkTask('t1'), mkTask('t3', 'PENDING', 'scope work again')],
      [],
      { revisionKind: 'off_topic', revisionReason: 'refocus' },
    );
    const links = __internal.deriveSupersedes([rev0, rev1], new Map());
    expect(links.size).toBe(1);
    const link = links.get('t2') as SupersessionLink;
    expect(link.newTaskId).toBe('t3');
    expect(link.kind).toBe('off_topic');
  });
});

describe('<TaskStagesGraph /> cumulative mode (collapsed chains)', () => {
  it('collapses a supersedes chain into one canonical card; singleton tasks render as their own card', () => {
    const store = seedTwoRevPlan();
    const cum = __internal.deriveCumulative('plan-1', store.tasks.allRevsForPlan('plan-1'));
    const supersedes = __internal.deriveSupersedes(
      store.tasks.allRevsForPlan('plan-1'),
      new Map([['drift-abc', store.drifts.list()[0]]]),
    );
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [])}
        cumulative={cum}
        supersedesMap={supersedes}
      />,
    );
    const cards = container.querySelectorAll('g.hg-stages__card');
    // t1 (singleton) + {t2, t3} collapsed into one chain card = 2 cards.
    expect(cards.length).toBe(2);
  });

  it('attaches one corner rev chip per canonical card (singletons and chains alike)', () => {
    const store = seedTwoRevPlan();
    const cum = __internal.deriveCumulative('plan-1', store.tasks.allRevsForPlan('plan-1'));
    const supersedes = __internal.deriveSupersedes(
      store.tasks.allRevsForPlan('plan-1'),
      new Map([['drift-abc', store.drifts.list()[0]]]),
    );
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [])}
        cumulative={cum}
        supersedesMap={supersedes}
      />,
    );
    // After the plan-view redesign every canonical card carries a
    // single corner chip — singleton t1 (muted R0 label) and the
    // {t2,t3} canonical t3 (R0→R1 chain label). One axis, one chip.
    const chips = container.querySelectorAll('[data-testid^="rev-chip-for-"]');
    expect(chips.length).toBe(2);
    const byTask = new Map<string, Element>();
    chips.forEach((c) => {
      const id = c.getAttribute('data-testid')!.replace('rev-chip-for-', '');
      byTask.set(id, c);
    });
    expect(byTask.has('t1')).toBe(true);
    expect(byTask.has('t3')).toBe(true);
  });

  it('no longer renders the retired REV N generation badge', () => {
    const store = seedTwoRevPlan();
    const cum = __internal.deriveCumulative('plan-1', store.tasks.allRevsForPlan('plan-1'));
    const supersedes = __internal.deriveSupersedes(
      store.tasks.allRevsForPlan('plan-1'),
      new Map([['drift-abc', store.drifts.list()[0]]]),
    );
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [])}
        cumulative={cum}
        supersedesMap={supersedes}
      />,
    );
    // The R0→R1 arrow badge + separate "REV 1" gen-badge are gone;
    // provenance collapses onto the single corner chip.
    expect(container.querySelectorAll('g.hg-stages__gen-badge').length).toBe(0);
    expect(container.querySelectorAll('[data-testid="gen-badge"]').length).toBe(0);
  });

  it('no longer renders visible "supersedes-edge" elements (chain badge replaces them)', () => {
    const store = seedTwoRevPlan();
    const cum = __internal.deriveCumulative('plan-1', store.tasks.allRevsForPlan('plan-1'));
    const supersedes = __internal.deriveSupersedes(
      store.tasks.allRevsForPlan('plan-1'),
      new Map([['drift-abc', store.drifts.list()[0]]]),
    );
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [])}
        cumulative={cum}
        supersedesMap={supersedes}
      />,
    );
    expect(container.querySelectorAll('[data-testid="supersedes-edge"]')).toHaveLength(0);
  });
});

describe('<TaskPlanPanel /> integration (collapsed chains)', () => {
  it('defaults to cumulative view and renders one card per logical slot after collapse', () => {
    const store = seedTwoRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    render(<TaskPlanPanel sessionId="sess-1" plan={plan} />);
    expect(screen.getByTestId('task-stages-graph')).toBeInTheDocument();
    // Post-redesign: the Gantt subview no longer owns a local scrubber.
    // Rev selection lives in the Trajectory view; the subview mirrors
    // the shared `selectedRevision` state instead.
    expect(screen.queryByTestId('plan-revision-scrubber')).toBeNull();
    // cumulative → t1 + {t2,t3}-chain = 2 cards after collapse.
    const cards = document
      .querySelector('[data-testid="task-stages-graph"]')!
      .querySelectorAll('g.hg-stages__card');
    expect(cards.length).toBe(2);
  });

  it('shared selectedRevision pinning hides chains whose earliest member is introduced after the pinned rev', () => {
    const store = seedThreeRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    // Simulate the Trajectory view (or ribbon) pinning rev 0 via the
    // shared uiStore slice. The Gantt subview has no local control; it
    // reads this on render.
    useUiStore.getState().setSelectedRevision(0);
    render(<TaskPlanPanel sessionId="sess-3" plan={plan} />);
    // t1 was intro'd at rev 0 → visible. t2 (rev 1) + t3 (rev 2) are
    // singleton chains whose firstRev > 0 → hidden entirely (no ghost).
    const cards = document
      .querySelector('[data-testid="task-stages-graph"]')!
      .querySelectorAll('g.hg-stages__card');
    expect(cards.length).toBe(1);
    // The sync-hint callout mirrors the Trajectory selection.
    expect(screen.getByTestId('plan-sync-hint').textContent).toContain(
      'REV 0',
    );
    // Restore latest so other tests start clean.
    useUiStore.getState().setSelectedRevision(null);
  });

  it('Latest-only toggle drops the cumulative rendering (no rev chips, no gen badges)', () => {
    const store = seedTwoRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    render(<TaskPlanPanel sessionId="sess-1" plan={plan} />);
    // Switch to Latest only.
    const toggle = screen.getByTestId('task-plan-view-toggle') as HTMLSelectElement;
    fireEvent.change(toggle, { target: { value: 'latest' } });
    // Latest only → just t1 + t3 (the live rev 1 shape). No rev chips
    // (Latest-only has no rev meta to surface), no legacy gen badges.
    const cards = document
      .querySelector('[data-testid="task-stages-graph"]')!
      .querySelectorAll('g.hg-stages__card');
    expect(cards.length).toBe(2);
    expect(
      document.querySelectorAll('[data-testid^="rev-chip-for-"]'),
    ).toHaveLength(0);
    expect(document.querySelectorAll('g.hg-stages__gen-badge')).toHaveLength(0);
    // Restore the default so later describes aren't stuck on 'latest'.
    fireEvent.change(toggle, { target: { value: 'cumulative' } });
  });
});
