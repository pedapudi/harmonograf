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

  it('renders one column per stage with "N/M" progress badges', () => {
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
    render(<TaskStagesGraph plan={plan} />);

    expect(screen.getByTestId('task-stages-graph')).toBeInTheDocument();

    const labels = screen.getAllByText(/^Stage \d+$/);
    expect(labels.map((n) => n.textContent)).toEqual([
      'Stage 0',
      'Stage 1',
      'Stage 2',
    ]);

    expect(screen.getByText('1/1')).toBeInTheDocument();
    const zeroOfOne = screen.getAllByText('0/1');
    expect(zeroOfOne.length).toBeGreaterThanOrEqual(2);
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
});
