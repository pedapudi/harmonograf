// TaskNodeDetail — pure content component for the trajectory task-node
// detail pane. Extracted from TrajectoryView.DetailPane so it can be
// mounted inside a `<TrajectoryFloatingDrawer>` by the layout-restructure
// worktree without pulling in the rest of the view's state.
//
// Props are intentionally plain: the caller hands in the resolved task +
// plan + optional jump handler. The component itself does not reach into
// the UI store, so it stays easy to test and reuse.

import type React from 'react';
import { bareAgentName } from '../../../gantt/index';
import type { SessionStore } from '../../../gantt/index';
import type { Task, TaskPlan } from '../../../gantt/types';

export interface TaskNodeDetailProps {
  task: Task;
  plan: TaskPlan;
  /** Optional store for agent-name lookup + bound-span start time. */
  store?: SessionStore | null;
  /**
   * Called when the user presses the "Jump to Gantt" button. Parent
   * decides how to navigate (typically selectSpan on the task's bound
   * span or scrolling the Gantt to the task's row).
   */
  onJumpToGantt?: (taskId: string) => void;
}

function fmtTime(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function TaskNodeDetail(props: TaskNodeDetailProps): React.ReactElement {
  const { task, plan, store, onJumpToGantt } = props;
  const span =
    task.boundSpanId && store ? store.spans.get(task.boundSpanId) : null;
  const assigneeName = task.assigneeAgentId
    ? store?.agents.get(task.assigneeAgentId)?.name ||
      bareAgentName(task.assigneeAgentId) ||
      task.assigneeAgentId
    : '—';
  // Keep the plan reference live — currently unused beyond context, but
  // callers pass it so future enhancements (sibling-task links, edge
  // summary) can use it without a prop signature change.
  void plan;

  return (
    <div className="hg-traj__task-detail" data-testid="task-node-detail">
      <header className="hg-traj__task-detail-head">
        <span className="hg-traj__detail-kind">task</span>
        <h3 data-testid="task-node-detail-title">{task.title || task.id}</h3>
        <span
          className="hg-traj__detail-status"
          data-status={task.status}
          data-testid="task-node-detail-status"
        >
          {task.status.toLowerCase()}
        </span>
      </header>
      {task.description && (
        <p
          className="hg-traj__detail-desc"
          data-testid="task-node-detail-description"
        >
          {task.description}
        </p>
      )}
      <dl className="hg-traj__detail-meta">
        <dt>assignee</dt>
        <dd
          data-testid="task-node-detail-assignee"
          title={task.assigneeAgentId || undefined}
        >
          {assigneeName}
        </dd>
        <dt>bound span</dt>
        <dd data-testid="task-node-detail-bound-span">
          {task.boundSpanId ? task.boundSpanId.slice(0, 8) : '—'}
        </dd>
        {span && (
          <>
            <dt>started</dt>
            <dd data-testid="task-node-detail-started">{fmtTime(span.startMs)}</dd>
          </>
        )}
      </dl>
      {(task.status === 'CANCELLED' || task.status === 'FAILED') &&
        task.cancelReason && (
          <section
            className="hg-traj__detail-cancel-reason"
            data-testid="task-node-detail-cancel-reason"
          >
            <strong>
              {task.status === 'FAILED' ? 'failed:' : 'cancelled:'}
            </strong>{' '}
            <span>{task.cancelReason}</span>
          </section>
        )}
      {onJumpToGantt && (
        <div className="hg-traj__task-detail-foot">
          <button
            type="button"
            className="hg-traj__task-detail-jump"
            data-testid="task-node-detail-jump-gantt"
            onClick={() => onJumpToGantt(task.id)}
          >
            Jump to Gantt
          </button>
        </div>
      )}
    </div>
  );
}
