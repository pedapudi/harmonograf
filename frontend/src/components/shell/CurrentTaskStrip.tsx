import { useEffect, useReducer } from 'react';
import { useUiStore } from '../../state/uiStore';
import { getSessionStore } from '../../rpc/hooks';
import type { ExecutionMode, Task, TaskStatus } from '../../gantt/types';
import { readExecutionMode } from '../../gantt/types';
import { bareAgentName } from '../../gantt/index';

const STATUS_CLASS: Record<TaskStatus, string> = {
  UNSPECIFIED: 'hg-strip__chip--pending',
  PENDING: 'hg-strip__chip--pending',
  RUNNING: 'hg-strip__chip--running',
  COMPLETED: 'hg-strip__chip--completed',
  FAILED: 'hg-strip__chip--failed',
  CANCELLED: 'hg-strip__chip--cancelled',
  BLOCKED: 'hg-strip__chip--pending',
};

const MODE_LABEL: Record<ExecutionMode, string> = {
  sequential: 'SEQ',
  parallel: 'PAR',
  delegated: 'OBS',
};

const MODE_TOOLTIP: Record<ExecutionMode, string> = {
  sequential:
    'Sequential mode — single-pass coordinator LLM executes the full plan, lifecycle reported via reporting tools',
  parallel:
    'Parallel mode — rigid DAG batch walker drives sub-agents directly, respecting plan edge dependencies',
  delegated:
    'Delegated (observer) mode — the inner agent owns its sequencing; harmonograf only watches for drift',
};

const MODE_CLASS: Record<ExecutionMode, string> = {
  sequential: 'hg-strip__mode--sequential',
  parallel: 'hg-strip__mode--parallel',
  delegated: 'hg-strip__mode--delegated',
};

// Slim strip mounted directly below the AppBar that shows the LIVE current
// task across the whole session — RUNNING takes precedence, otherwise falls
// back to the most-recently-completed task so the strip stays informative
// in between RUNNING transitions. Subscribes to the session's TaskRegistry
// so task-status updates repaint immediately.
export function CurrentTaskStrip() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const store = getSessionStore(sessionId);
  const [, bump] = useReducer((x: number) => x + 1, 0);

  useEffect(() => {
    if (!store) return;
    const unsubTasks = store.tasks.subscribe(() => bump());
    const unsubSpans = store.spans.subscribe(() => bump());
    // The mode chip is sourced from the assignee's metadata, which arrives via
    // the AgentRegistry on Hello — re-render when an agent's metadata lands.
    const unsubAgents = store.agents.subscribe(() => bump());
    return () => {
      unsubTasks();
      unsubSpans();
      unsubAgents();
    };
  }, [store]);

  const current = store ? store.getCurrentTask() : null;
  if (!current) return null;

  const task: Task = current.task;
  const status = task.status ?? 'PENDING';
  const running = status === 'RUNNING';
  const inFlightTool = current.inFlightTool;
  const isThinking = current.isThinking;
  const assignee = task.assigneeAgentId
    ? store?.agents.get(task.assigneeAgentId)
    : undefined;
  const mode = readExecutionMode(assignee);
  // Display preference: the registry name (bare) when present, else
  // derive the bare form directly from the compound id. Final fallback
  // is the raw id itself so we never render "undefined" when the id is
  // an empty string (the outer truthy-guard already skips that branch,
  // but be defensive). See harmonograf#133.
  const assigneeDisplay = task.assigneeAgentId
    ? assignee?.name || bareAgentName(task.assigneeAgentId) || task.assigneeAgentId
    : '';

  return (
    <div
      className="hg-strip"
      data-testid="current-task-strip"
      data-running={running ? 'true' : 'false'}
    >
      <span className="hg-strip__label">Currently:</span>
      <span className="hg-strip__title" title={task.description || task.title}>
        {task.title || task.id}
      </span>
      <span className={`hg-strip__chip ${STATUS_CLASS[status]}`}>{status}</span>
      {mode && (
        <span
          className={`hg-strip__mode ${MODE_CLASS[mode]}`}
          data-testid="current-task-strip-mode"
          data-mode={mode}
          title={MODE_TOOLTIP[mode]}
          aria-label={MODE_TOOLTIP[mode]}
        >
          {MODE_LABEL[mode]}
        </span>
      )}
      {task.assigneeAgentId && (
        <span className="hg-strip__agent" title={`assigned to ${task.assigneeAgentId}`}>
          {assigneeDisplay}
          {isThinking && (
            <span
              className="hg-strip__thinking"
              data-testid="current-task-strip-thinking"
              title="agent is thinking"
              aria-label="thinking"
            />
          )}
        </span>
      )}
      {inFlightTool && (
        <span
          className="hg-strip__tool"
          data-testid="current-task-strip-tool"
          title={`in-flight tool: ${inFlightTool.name}`}
        >
          {inFlightTool.name}
        </span>
      )}
    </div>
  );
}
