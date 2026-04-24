/**
 * TrajectoryFloatingDrawer + SteeringDetailBody + TaskNodeDetail tests.
 *
 * Covers the floating-drawer refactor of the trajectory detail pane:
 *   - drawer visibility / slide transform / backdrop + esc close
 *   - focus trap + focus restore
 *   - SteeringDetailBody section testids match the legacy panel (so the
 *     TrajectoryView integration tests keep passing)
 *   - TaskNodeDetail metadata + jump-to-gantt handler
 *   - Integration: SteeringDetailBody mounted inside the drawer
 */

import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { TrajectoryFloatingDrawer } from '../../components/shell/views/TrajectoryFloatingDrawer';
import {
  SteeringDetailBody,
  type SteeringSelection,
} from '../../components/shell/views/SteeringDetailPanel';
import { TaskNodeDetail } from '../../components/shell/views/TaskNodeDetail';
import type { Task, TaskPlan } from '../../gantt/types';
import type {
  PlanRevisionRecord,
  SupersessionLink,
} from '../../state/planHistoryStore';

vi.mock('../../components/shell/views/views.css', () => ({}));

// ── Fixtures ──────────────────────────────────────────────────────────────

function mkTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 't1',
    title: 'build outline',
    description: 'lay out the doc structure',
    assigneeAgentId: 'agent-a',
    status: 'RUNNING',
    predictedStartMs: 0,
    predictedDurationMs: 0,
    boundSpanId: 'span-abcdef12',
    cancelReason: '',
    supersedes: '',
    ...overrides,
  };
}

function mkPlan(tasks: Task[]): TaskPlan {
  return {
    id: 'p1',
    invocationSpanId: 'inv-p1',
    plannerAgentId: 'planner',
    createdAtMs: 0,
    summary: 'plan p1',
    tasks,
    edges: [],
    revisionReason: '',
  };
}

function mkRevisionRecord(overrides: Partial<PlanRevisionRecord> = {}): PlanRevisionRecord {
  return {
    revision: 1,
    plan: mkPlan([mkTask()]),
    reason: 'coordinator drifted off topic',
    kind: 'off_topic',
    triggerEventId: '',
    emittedAtMs: 0,
    ...overrides,
  };
}

// ── Drawer base ───────────────────────────────────────────────────────────

describe('TrajectoryFloatingDrawer', () => {
  it('renders nothing when open=false', () => {
    render(
      <TrajectoryFloatingDrawer open={false} onClose={() => {}}>
        <button>inside</button>
      </TrajectoryFloatingDrawer>,
    );
    expect(screen.queryByTestId('trajectory-drawer')).not.toBeInTheDocument();
  });

  it('renders dialog with role/aria when open=true', () => {
    render(
      <TrajectoryFloatingDrawer open={true} onClose={() => {}} title="Detail">
        <button>inside</button>
      </TrajectoryFloatingDrawer>,
    );
    const drawer = screen.getByTestId('trajectory-drawer');
    expect(drawer).toHaveAttribute('role', 'dialog');
    expect(drawer).toHaveAttribute('aria-modal', 'true');
    expect(drawer).toHaveAttribute('aria-labelledby');
    const titleEl = document.getElementById(drawer.getAttribute('aria-labelledby')!);
    expect(titleEl?.textContent).toBe('Detail');
  });

  it('applies slide-in transform (animation class present on open)', () => {
    render(
      <TrajectoryFloatingDrawer open={true} onClose={() => {}}>
        <div>body</div>
      </TrajectoryFloatingDrawer>,
    );
    const drawer = screen.getByTestId('trajectory-drawer');
    expect(drawer).toHaveAttribute('data-open', 'true');
    // transform is set to translateX(0) by CSS; we assert the drawer class
    // is present so the ~200ms slide-in keyframe applies.
    expect(drawer.className).toContain('hg-traj__drawer');
  });

  it('backdrop click invokes onClose', () => {
    const onClose = vi.fn();
    render(
      <TrajectoryFloatingDrawer open={true} onClose={onClose}>
        <div>body</div>
      </TrajectoryFloatingDrawer>,
    );
    fireEvent.click(screen.getByTestId('trajectory-drawer-backdrop'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('Escape key invokes onClose', () => {
    const onClose = vi.fn();
    render(
      <TrajectoryFloatingDrawer open={true} onClose={onClose}>
        <div>body</div>
      </TrajectoryFloatingDrawer>,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('close button in header invokes onClose', () => {
    const onClose = vi.fn();
    render(
      <TrajectoryFloatingDrawer open={true} onClose={onClose} title="T">
        <div>body</div>
      </TrajectoryFloatingDrawer>,
    );
    fireEvent.click(screen.getByTestId('trajectory-drawer-close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('focus trap: Tab at last element wraps to first, Shift-Tab at first wraps to last', () => {
    render(
      <TrajectoryFloatingDrawer open={true} onClose={() => {}}>
        <button data-testid="a">A</button>
        <button data-testid="b">B</button>
        <button data-testid="c">C</button>
      </TrajectoryFloatingDrawer>,
    );
    const a = screen.getByTestId('a');
    const c = screen.getByTestId('c');
    c.focus();
    expect(document.activeElement).toBe(c);
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(document.activeElement).toBe(a);
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(c);
  });

  it('focus restored to trigger on close', () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <div>
          <button data-testid="trigger" onClick={() => setOpen(true)}>
            open
          </button>
          <TrajectoryFloatingDrawer open={open} onClose={() => setOpen(false)}>
            <button data-testid="inside-btn">inside</button>
          </TrajectoryFloatingDrawer>
        </div>
      );
    }
    render(<Harness />);
    const trigger = screen.getByTestId('trigger');
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    fireEvent.click(trigger);
    // Drawer mounted; focus moved inside.
    expect(document.activeElement).toBe(screen.getByTestId('inside-btn'));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('trajectory-drawer')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });
});

// ── SteeringDetailBody ────────────────────────────────────────────────────

describe('SteeringDetailBody', () => {
  const selection: SteeringSelection = {
    kind: 'revision',
    revision: 1,
    targetTaskId: 't1',
  };
  const plan = mkPlan([mkTask({ id: 't1', title: 'research paper', assigneeAgentId: 'research_agent' })]);
  const history: PlanRevisionRecord[] = [mkRevisionRecord({ plan })];
  const supersedes = new Map<string, SupersessionLink>();

  it('renders Trigger / Steering / Target sections with preserved testids', () => {
    render(
      <SteeringDetailBody
        selection={selection}
        plan={plan}
        history={history}
        supersedes={supersedes}
        store={null}
        onJumpToGantt={() => {}}
      />,
    );
    expect(screen.getByTestId('steering-detail-body')).toBeInTheDocument();
    expect(screen.getByTestId('steering-detail-trigger')).toBeInTheDocument();
    expect(screen.getByTestId('steering-detail-steering')).toBeInTheDocument();
    expect(screen.getByTestId('steering-detail-target')).toBeInTheDocument();
    expect(screen.getByTestId('steering-detail-target-agent')).toHaveTextContent(
      'research_agent',
    );
    expect(screen.getByTestId('steering-detail-target-task')).toHaveTextContent(
      'research paper',
    );
    expect(screen.getByTestId('steering-detail-trigger')).toHaveTextContent(
      'off_topic',
    );
  });

  it('jump-to-gantt is disabled when no triggerEventId and fires callback otherwise', () => {
    const onJump = vi.fn();
    const { rerender } = render(
      <SteeringDetailBody
        selection={selection}
        plan={plan}
        history={history}
        supersedes={supersedes}
        store={null}
        onJumpToGantt={onJump}
      />,
    );
    const btn = screen.getByTestId('steering-detail-jump-gantt');
    expect(btn).toBeDisabled();

    rerender(
      <SteeringDetailBody
        selection={selection}
        plan={plan}
        history={[mkRevisionRecord({ plan, triggerEventId: 'drift-xyz' })]}
        supersedes={supersedes}
        store={null}
        onJumpToGantt={onJump}
      />,
    );
    const btn2 = screen.getByTestId('steering-detail-jump-gantt');
    expect(btn2).not.toBeDisabled();
    fireEvent.click(btn2);
    expect(onJump).toHaveBeenCalledTimes(1);
    expect(onJump).toHaveBeenCalledWith(null, 'drift-xyz');
  });
});

// ── TaskNodeDetail ────────────────────────────────────────────────────────

describe('TaskNodeDetail', () => {
  it('renders assignee / description / status / bound span', () => {
    const task = mkTask({
      assigneeAgentId: 'spec://research_agent',
      description: 'do the research',
      status: 'RUNNING',
      boundSpanId: 'deadbeef1234',
    });
    render(<TaskNodeDetail task={task} plan={mkPlan([task])} store={null} />);
    expect(screen.getByTestId('task-node-detail-title')).toHaveTextContent(
      'build outline',
    );
    expect(screen.getByTestId('task-node-detail-status')).toHaveTextContent('running');
    expect(screen.getByTestId('task-node-detail-description')).toHaveTextContent(
      'do the research',
    );
    // First 8 chars of bound span id.
    expect(screen.getByTestId('task-node-detail-bound-span')).toHaveTextContent(
      'deadbeef',
    );
    // Assignee falls back to bareAgentName / raw id when no store entry.
    expect(screen.getByTestId('task-node-detail-assignee')).toBeInTheDocument();
  });

  it('renders cancel reason for CANCELLED tasks', () => {
    const task = mkTask({ status: 'CANCELLED', cancelReason: 'superseded_by_revision' });
    render(<TaskNodeDetail task={task} plan={mkPlan([task])} />);
    expect(screen.getByTestId('task-node-detail-cancel-reason')).toHaveTextContent(
      'superseded_by_revision',
    );
  });

  it('jump-to-gantt button calls onJumpToGantt with the task id', () => {
    const task = mkTask({ id: 'task-7' });
    const onJump = vi.fn();
    render(
      <TaskNodeDetail task={task} plan={mkPlan([task])} onJumpToGantt={onJump} />,
    );
    fireEvent.click(screen.getByTestId('task-node-detail-jump-gantt'));
    expect(onJump).toHaveBeenCalledWith('task-7');
  });

  it('omits jump-to-gantt button when no handler supplied', () => {
    const task = mkTask();
    render(<TaskNodeDetail task={task} plan={mkPlan([task])} />);
    expect(screen.queryByTestId('task-node-detail-jump-gantt')).not.toBeInTheDocument();
  });
});

// ── Integration: body mounted inside drawer ──────────────────────────────

describe('SteeringDetailBody inside TrajectoryFloatingDrawer', () => {
  it('open/close works end-to-end; body visible only while open', () => {
    const selection: SteeringSelection = {
      kind: 'revision',
      revision: 1,
      targetTaskId: 't1',
    };
    const plan = mkPlan([mkTask({ id: 't1' })]);
    const history: PlanRevisionRecord[] = [mkRevisionRecord({ plan })];
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <div>
          <button data-testid="opener" onClick={() => setOpen(true)}>
            open
          </button>
          <TrajectoryFloatingDrawer
            open={open}
            onClose={() => setOpen(false)}
            title="Steering"
            testId="integration-drawer"
            closeTestId="integration-drawer-close"
          >
            <SteeringDetailBody
              selection={selection}
              plan={plan}
              history={history}
              supersedes={new Map()}
              store={null}
              onJumpToGantt={() => {}}
            />
          </TrajectoryFloatingDrawer>
        </div>
      );
    }
    render(<Harness />);
    expect(screen.queryByTestId('steering-detail-body')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('opener'));
    const drawer = screen.getByTestId('integration-drawer');
    expect(within(drawer).getByTestId('steering-detail-body')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('integration-drawer-close'));
    expect(screen.queryByTestId('steering-detail-body')).not.toBeInTheDocument();
  });
});
