import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
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

// Imported AFTER the mock so the hooks bind to our fake getSessionStore.
import { TaskStagesGraph } from '../../components/TaskStages/TaskStagesGraph';
import { TaskPlanPanel } from '../../components/TaskStages/TaskPlanPanel';
import {
  __internal,
  type CumulativePlan,
  type SupersessionLink,
} from '../../state/planHistory';

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

describe('<TaskStagesGraph /> cumulative mode', () => {
  it('renders both live and superseded tasks with muted styling for superseded', () => {
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
    expect(cards.length).toBe(3); // t1, t2 (superseded), t3
    const superseded = Array.from(cards).filter(
      (c) => c.getAttribute('data-superseded') === 'true',
    );
    expect(superseded).toHaveLength(1);
  });

  it('renders one generation badge per task with the right revision number', () => {
    const store = seedTwoRevPlan();
    const cum = __internal.deriveCumulative('plan-1', store.tasks.allRevsForPlan('plan-1'));
    const { container } = render(
      <TaskStagesGraph plan={mkPlan(0, [])} cumulative={cum} />,
    );
    const badges = container.querySelectorAll('g.hg-stages__gen-badge');
    expect(badges.length).toBe(3);
    const revs = Array.from(badges).map((b) => b.getAttribute('data-rev'));
    expect(revs.sort()).toEqual(['0', '0', '1']);
  });

  it('renders a supersedes edge with a drift-kind annotation', () => {
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
    const edges = container.querySelectorAll('[data-testid="supersedes-edge"]');
    expect(edges.length).toBe(1);
    const label = edges[0].querySelector('.hg-stages__supersedes-label');
    expect(label?.textContent).toContain('off_topic');
    expect(label?.textContent).toContain('goldfive');
  });

  it('fires onSupersedesEdgeClick with the link payload when clicked', () => {
    const store = seedTwoRevPlan();
    const cum = __internal.deriveCumulative('plan-1', store.tasks.allRevsForPlan('plan-1'));
    const supersedes = __internal.deriveSupersedes(
      store.tasks.allRevsForPlan('plan-1'),
      new Map([['drift-abc', store.drifts.list()[0]]]),
    );
    const handler = vi.fn();
    const { container } = render(
      <TaskStagesGraph
        plan={mkPlan(0, [])}
        cumulative={cum}
        supersedesMap={supersedes}
        onSupersedesEdgeClick={handler}
      />,
    );
    const edge = container.querySelector('[data-testid="supersedes-edge"]');
    expect(edge).toBeTruthy();
    fireEvent.click(edge!);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0][0].oldTaskId).toBe('t2');
    expect(handler.mock.calls[0][0].newTaskId).toBe('t3');
  });
});

describe('<TaskPlanPanel /> integration', () => {
  it('defaults to cumulative view and renders the scrubber + both task revs', () => {
    const store = seedTwoRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    render(<TaskPlanPanel sessionId="sess-1" plan={plan} />);
    expect(screen.getByTestId('task-stages-graph')).toBeInTheDocument();
    expect(screen.getByTestId('plan-revision-scrubber')).toBeInTheDocument();
    // cumulative → shows t1, t2 (muted), t3
    const cards = document
      .querySelector('[data-testid="task-stages-graph"]')!
      .querySelectorAll('g.hg-stages__card');
    expect(cards.length).toBe(3);
  });

  it('opens the steering-detail side panel on supersedes-edge click', () => {
    const store = seedTwoRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    render(<TaskPlanPanel sessionId="sess-1" plan={plan} />);
    const edges = document.querySelectorAll('[data-testid="supersedes-edge"]');
    expect(edges.length).toBe(1);
    fireEvent.click(edges[0]);
    const panel = screen.getByTestId('steering-detail-panel');
    expect(panel).toBeInTheDocument();
    // Trigger / Steering / Target sections populated.
    expect(screen.getByTestId('steering-detail-trigger').textContent).toContain(
      'off_topic',
    );
    expect(screen.getByTestId('steering-detail-steering').textContent).toContain(
      'goldfive',
    );
    expect(screen.getByTestId('steering-detail-target').textContent).toContain('agent-a');
  });

  it('revision scrubber filter mutes tasks introduced in later revisions', () => {
    const store = seedThreeRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    render(<TaskPlanPanel sessionId="sess-3" plan={plan} />);
    // Default selection is 'Latest' → nothing muted from the scrubber
    // (cards may still be superseded in the no-superseded path here; t3
    // is the tail so no tasks are superseded). Click rev 0.
    fireEvent.click(screen.getByTestId('scrubber-notch-0'));
    const cards = document
      .querySelector('[data-testid="task-stages-graph"]')!
      .querySelectorAll('g.hg-stages__card');
    const muted = Array.from(cards).filter((c) =>
      c.classList.contains('hg-stages__card--muted'),
    );
    // t2 + t3 were introduced after rev 0 → muted.
    expect(muted.length).toBe(2);
  });

  it('Latest-only toggle drops the cumulative rendering (no supersedes edges)', () => {
    const store = seedTwoRevPlan();
    const plan = store.tasks.getPlan('plan-1')!;
    render(<TaskPlanPanel sessionId="sess-1" plan={plan} />);
    // Switch to Latest only.
    const toggle = screen.getByTestId('task-plan-view-toggle') as HTMLSelectElement;
    fireEvent.change(toggle, { target: { value: 'latest' } });
    // Latest only → just t1 + t3 (the live rev 1 shape). No gen badges.
    const cards = document
      .querySelector('[data-testid="task-stages-graph"]')!
      .querySelectorAll('g.hg-stages__card');
    expect(cards.length).toBe(2);
    expect(
      document.querySelectorAll('[data-testid="supersedes-edge"]'),
    ).toHaveLength(0);
    expect(document.querySelectorAll('g.hg-stages__gen-badge')).toHaveLength(0);
  });
});
