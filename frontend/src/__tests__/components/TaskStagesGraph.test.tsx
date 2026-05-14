import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { TaskStagesGraph } from '../../components/TaskStages/TaskStagesGraph';
import type { Task, TaskEdge, TaskPlan, TaskStatus } from '../../gantt/types';

vi.mock('../../components/TaskStages/TaskStagesGraph.css', () => ({}));

function mkTask(id: string, status: TaskStatus = 'PENDING'): Task {
  return {
    id,
    title: `Title ${id}`,
    description: '',
    assigneeAgentId: 'agent-a',
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    supersedes: '',
  };
}

function mkPlan(tasks: Task[], edges: Array<[string, string]> = []): TaskPlan {
  return {
    id: 'plan-1',
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks,
    edges: edges.map<TaskEdge>(([f, t]) => ({ fromTaskId: f, toTaskId: t })),
    revisionReason: '',
  };
}

describe('<TaskStagesGraph />', () => {
  it('renders null for an empty plan', () => {
    const { container } = render(<TaskStagesGraph plan={mkPlan([])} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders one column per stage with a plain "Stage N" header (no ambiguous X/Y counter)', () => {
    const plan = mkPlan(
      [
        mkTask('t1', 'COMPLETED'),
        mkTask('t2', 'RUNNING'),
        mkTask('t3', 'PENDING'),
      ],
      [
        ['t1', 't2'],
        ['t2', 't3'],
      ],
    );
    const { container } = render(<TaskStagesGraph plan={plan} />);

    expect(screen.getByTestId('task-stages-graph')).toBeInTheDocument();

    const labels = screen.getAllByText(/^Stage \d+$/);
    expect(labels.map((n) => n.textContent)).toEqual([
      'Stage 0',
      'Stage 1',
      'Stage 2',
    ]);

    // The "0/1" / "1/1" progress counter has been removed — its unit was
    // ambiguous and card fill already encodes progress.
    expect(screen.queryByText('1/1')).toBeNull();
    expect(screen.queryByText('0/1')).toBeNull();
    expect(container.querySelectorAll('.hg-stages__progress').length).toBe(0);
  });

  it('fires onTaskClick when a card is clicked', () => {
    const onTaskClick = vi.fn();
    const plan = mkPlan([mkTask('t1'), mkTask('t2')], [['t1', 't2']]);
    const { container } = render(
      <TaskStagesGraph plan={plan} onTaskClick={onTaskClick} />,
    );
    const cardGroup = container.querySelectorAll('g.hg-stages__card')[0];
    expect(cardGroup).toBeTruthy();
    fireEvent.click(cardGroup!);
    expect(onTaskClick).toHaveBeenCalledTimes(1);
    expect(onTaskClick.mock.calls[0][0].id).toBe('t1');
  });

  it('paints status dots with the color matching each status', () => {
    const plan = mkPlan(
      [
        mkTask('run', 'RUNNING'),
        mkTask('done', 'COMPLETED'),
        mkTask('fail', 'FAILED'),
      ],
      [],
    );
    const { container } = render(<TaskStagesGraph plan={plan} />);
    const circles = container.querySelectorAll('circle');
    const fills = Array.from(circles).map((c) => c.getAttribute('fill'));
    expect(fills).toContain('#5b8def');
    expect(fills).toContain('#4caf50');
    expect(fills).toContain('#e06070');
  });

  it('keeps a stable card count with a mix of stages', () => {
    const plan = mkPlan(
      ['t1', 't2', 't3', 't4'].map((id) => mkTask(id)),
      [
        ['t1', 't2'],
        ['t1', 't3'],
        ['t2', 't4'],
        ['t3', 't4'],
      ],
    );
    const { container } = render(<TaskStagesGraph plan={plan} />);
    const cardRects = container.querySelectorAll('rect.hg-stages__card-rect');
    expect(cardRects).toHaveLength(4);
  });

  // harmonograf#110 / goldfive#205: cancel reason tooltip on CANCELLED /
  // FAILED cards. Hovering the card's SVG <title> reveals the structured
  // reason so operators know "why was this task cancelled?" without a
  // click.
  it('embeds cancelReason in the tooltip for CANCELLED tasks', () => {
    const cancelled: Task = {
      ...mkTask('t1', 'CANCELLED'),
      cancelReason: 'upstream_failed:root_task',
    };
    const plan = mkPlan([cancelled], []);
    const { container } = render(<TaskStagesGraph plan={plan} />);
    const titles = container.querySelectorAll('g.hg-stages__card title');
    expect(titles).toHaveLength(1);
    expect(titles[0].textContent).toContain('upstream_failed:root_task');
  });

  it('embeds cancelReason in the tooltip for FAILED tasks', () => {
    const failed: Task = {
      ...mkTask('t1', 'FAILED'),
      cancelReason: 'refine_validation_failed',
    };
    const plan = mkPlan([failed], []);
    const { container } = render(<TaskStagesGraph plan={plan} />);
    const title = container.querySelector('g.hg-stages__card title');
    expect(title?.textContent).toContain('refine_validation_failed');
  });

  it('does not append cancelReason for non-terminal tasks', () => {
    // Regression guard: a stale cancelReason on a RUNNING task (e.g.
    // a later revision re-opened the task) must not bleed into the
    // tooltip.
    const running: Task = {
      ...mkTask('t1', 'RUNNING'),
      cancelReason: 'stale_reason_should_not_show',
    };
    const plan = mkPlan([running], []);
    const { container } = render(<TaskStagesGraph plan={plan} />);
    const title = container.querySelector('g.hg-stages__card title');
    expect(title?.textContent).not.toContain('stale_reason_should_not_show');
  });

  // harmonograf#107 — regression guard. The reported bug ("7-stage plan
  // with NO arrows between stages") can only happen if `plan.edges` arrives
  // empty. With a seeded 7-edge plan the SVG MUST produce 7 <path> nodes.
  it('renders one path element per edge on a 7-stage DAG', () => {
    const plan = mkPlan(
      ['a', 'b', 'c', 'd', 'e', 'f', 'g'].map((id) => mkTask(id)),
      [
        ['a', 'b'],
        ['b', 'c'],
        ['b', 'd'],
        ['c', 'e'],
        ['d', 'e'],
        ['e', 'f'],
        ['f', 'g'],
      ],
    );
    const { container } = render(<TaskStagesGraph plan={plan} />);
    const paths = container.querySelectorAll('path.hg-stages__edge');
    expect(paths).toHaveLength(7);
    // Every path carries a marker-end so the arrowhead renders.
    paths.forEach((p) =>
      expect(p.getAttribute('marker-end')).toBe('url(#hg-stages-arrow)'),
    );
  });

  // goldfive#423 PR 3 — plan-descriptive-growth rendering. Tasks installed
  // reactively at delegation-observed time (``discovered=true``) must
  // surface visually as distinct from forecast tasks. The tests assert:
  //   1. A plan with NO discovered tasks renders identically — same card
  //      count, no DISC badge, no ``--discovered`` class. This is the
  //      back-compat / visual-baseline check.
  //   2. A task with ``discovered=true`` carries the
  //      ``hg-stages__card--discovered`` class + a ``data-discovered``
  //      attribute, and a DISC pill text node is rendered.
  //   3. A mixed plan (some discovered, some not) renders exactly the
  //      expected subset with the discovery accent.
  //   4. A legacy fixture WITHOUT the ``discovered`` field at all
  //      (omitted, undefined) renders as non-discovered — this proves
  //      back-compat for proto frames that predate the field.
  describe('discovered-task rendering (goldfive#423)', () => {
    it('renders no DISC badge or accent class on a plan with no discovered tasks', () => {
      const plan = mkPlan([mkTask('t1'), mkTask('t2')], [['t1', 't2']]);
      const { container } = render(<TaskStagesGraph plan={plan} />);
      expect(container.querySelectorAll('g.hg-stages__card--discovered')).toHaveLength(0);
      expect(container.querySelectorAll('g.hg-stages__card-disc-badge')).toHaveLength(0);
      // Card count + class set are otherwise unchanged.
      expect(container.querySelectorAll('g.hg-stages__card')).toHaveLength(2);
    });

    it('renders a discovered task with the --discovered class + DISC badge', () => {
      const discovered: Task = { ...mkTask('t-disc'), discovered: true };
      const plan = mkPlan([discovered], []);
      const { container } = render(<TaskStagesGraph plan={plan} />);
      const cards = container.querySelectorAll('g.hg-stages__card');
      expect(cards).toHaveLength(1);
      expect(cards[0].classList.contains('hg-stages__card--discovered')).toBe(true);
      expect(cards[0].getAttribute('data-discovered')).toBe('true');
      // The DISC pill renders the literal text "DISC" inside a badge group.
      const badge = container.querySelector('g.hg-stages__card-disc-badge');
      expect(badge).toBeTruthy();
      expect(badge?.textContent).toContain('DISC');
      // testid is opt-in so downstream consumers can target it.
      expect(
        container.querySelector('[data-testid="task-card-discovered-t-disc"]'),
      ).toBeTruthy();
    });

    it('only marks discovered tasks in a mixed plan', () => {
      const forecast = mkTask('forecast');
      const discovered: Task = { ...mkTask('disc'), discovered: true };
      const plan = mkPlan([forecast, discovered], []);
      const { container } = render(<TaskStagesGraph plan={plan} />);
      const cardForecast = container.querySelector(
        'g.hg-stages__card[data-discovered]',
      );
      expect(cardForecast?.textContent).toContain('Title disc');
      // forecast card is NOT flagged
      const cardsByDisc = Array.from(
        container.querySelectorAll('g.hg-stages__card'),
      ).map((c) => c.getAttribute('data-discovered'));
      expect(cardsByDisc).toEqual([null, 'true']);
      expect(container.querySelectorAll('g.hg-stages__card-disc-badge')).toHaveLength(1);
    });

    it('renders a legacy task (no `discovered` field at all) as non-discovered', () => {
      // ``mkTask`` doesn't set ``discovered`` — the field is undefined.
      // The renderer must coerce undefined → false (back-compat path for
      // proto frames predating #423).
      const legacyTask = mkTask('legacy');
      // Defensive: strip the field if a future ``mkTask`` ever sets it.
      delete (legacyTask as { discovered?: boolean }).discovered;
      const plan = mkPlan([legacyTask], []);
      const { container } = render(<TaskStagesGraph plan={plan} />);
      expect(
        container.querySelectorAll('g.hg-stages__card--discovered'),
      ).toHaveLength(0);
      expect(
        container.querySelectorAll('g.hg-stages__card-disc-badge'),
      ).toHaveLength(0);
    });

    it('appends the (discovered at runtime) hint to the tooltip', () => {
      const discovered: Task = { ...mkTask('t1'), discovered: true };
      const plan = mkPlan([discovered], []);
      const { container } = render(<TaskStagesGraph plan={plan} />);
      const title = container.querySelector('g.hg-stages__card title');
      expect(title?.textContent).toContain('discovered at runtime');
    });
  });
});
