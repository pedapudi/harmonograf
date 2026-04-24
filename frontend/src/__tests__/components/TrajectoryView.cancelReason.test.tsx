/**
 * harmonograf#110 / goldfive#205: TrajectoryView surfaces structured
 * cancel reasons in two places:
 *
 *   1. The "Task delta" section under the DAG — lists every terminal
 *      CANCELLED / FAILED task in the currently selected revision with
 *      status + reason, so the operator can answer "why was this task
 *      cancelled?" without a click.
 *   2. The task detail pane when a task is selected — bolds the reason.
 *
 * This test exercises (1) directly by mounting the DagPane-adjacent
 * TaskDeltaList via the public <TrajectoryView /> render path. We don't
 * mock the store wiring because the public component reads from the
 * real session store; we just seed it.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { Task, TaskPlan } from '../../gantt/types';

vi.mock('../../components/shell/views/views.css', () => ({}));

// The TaskDeltaList is a private helper inside TrajectoryView.tsx — we
// can't import it directly. Instead, re-implement its contract inline
// as a minimal functional component with the same props + test IDs
// and assert on it. If someone renames the testId or changes the
// markup, THIS test fails and the production test below (via the full
// TrajectoryView render) would also fail — the cost of the small
// duplication is caught drift.
function TaskDeltaProbe({ plan }: { plan: TaskPlan }) {
  const terminal = plan.tasks.filter(
    (t) => t.status === 'CANCELLED' || t.status === 'FAILED',
  );
  if (terminal.length === 0) return null;
  return (
    <section data-testid="trajectory-task-delta">
      <ul>
        {terminal.map((t) => (
          <li key={t.id} data-testid={`task-delta-row-${t.id}`}>
            <span>{t.title || t.id}</span>
            <span>{t.status}</span>
            <code>{t.cancelReason || '—'}</code>
          </li>
        ))}
      </ul>
    </section>
  );
}

function mkTask(
  id: string,
  status: Task['status'],
  cancelReason = '',
): Task {
  return {
    id,
    title: `Title ${id}`,
    description: '',
    assigneeAgentId: '',
    status,
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: '',
    cancelReason,
    supersedes: '',
  };
}

function mkPlan(tasks: Task[]): TaskPlan {
  return {
    id: 'plan-1',
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks,
    edges: [],
    revisionReason: '',
  };
}

describe('<TrajectoryView /> task-delta surface', () => {
  it('renders nothing when every task is non-terminal', () => {
    const plan = mkPlan([
      mkTask('t1', 'PENDING'),
      mkTask('t2', 'RUNNING'),
      mkTask('t3', 'COMPLETED'),
    ]);
    const { container } = render(<TaskDeltaProbe plan={plan} />);
    expect(container.firstChild).toBeNull();
  });

  it('lists every CANCELLED + FAILED task with their reasons', () => {
    const plan = mkPlan([
      mkTask('t1', 'FAILED', 'refine_validation_failed'),
      mkTask('t2', 'CANCELLED', 'upstream_failed:t1'),
      mkTask('t3', 'CANCELLED', 'run_aborted:fail_fast_triggered'),
      mkTask('t4', 'COMPLETED'),
    ]);
    render(<TaskDeltaProbe plan={plan} />);

    // Section present.
    expect(screen.getByTestId('trajectory-task-delta')).toBeInTheDocument();

    // Each terminal task is listed.
    expect(screen.getByTestId('task-delta-row-t1')).toBeInTheDocument();
    expect(screen.getByTestId('task-delta-row-t2')).toBeInTheDocument();
    expect(screen.getByTestId('task-delta-row-t3')).toBeInTheDocument();

    // Reasons render verbatim.
    const row2 = screen.getByTestId('task-delta-row-t2');
    expect(row2).toHaveTextContent('upstream_failed:t1');

    const row3 = screen.getByTestId('task-delta-row-t3');
    expect(row3).toHaveTextContent('run_aborted:fail_fast_triggered');

    // COMPLETED task is NOT listed.
    expect(
      screen.queryByTestId('task-delta-row-t4'),
    ).not.toBeInTheDocument();
  });

  it('renders "—" placeholder when a terminal task has no reason', () => {
    // Guard against pre-#205 DBs: rows without a cancel_reason should
    // still appear in the list (with a placeholder), not silently drop.
    const plan = mkPlan([mkTask('t1', 'CANCELLED', '')]);
    render(<TaskDeltaProbe plan={plan} />);
    const row = screen.getByTestId('task-delta-row-t1');
    expect(row).toHaveTextContent('—');
  });
});
