import { useEffect, useRef, useState } from 'react';
import { GanttPlaceholder } from '../../Gantt/GanttPlaceholder';
import { TransportBar } from '../../TransportBar/TransportBar';
import { LiveActivityPanel } from '../../LiveActivity/LiveActivityPanel';
import { useUiStore, type TaskPlanMode } from '../../../state/uiStore';
import { useSessionWatch } from '../../../rpc/hooks';
import { colorForAgent } from '../../../theme/agentColors';
import type { Task, TaskPlan, TaskStatus } from '../../../gantt/types';
import { TaskPlanPanel } from '../../TaskStages/TaskPlanPanel';
import { InterventionsList } from '../../Interventions/InterventionsList';
import { deriveInterventionsFromStore } from '../../../lib/interventions';
import { useAnnotationStore } from '../../../state/annotationStore';

const DEFAULT_PANEL_HEIGHT = 120;
const MIN_PANEL_HEIGHT = 60;
const COLLAPSED_HEIGHT = 28;
const PANEL_HEIGHT_STORAGE_KEY = 'harmonograf.taskPanelHeight';

function loadStoredPanelHeight(): number {
  if (typeof window === 'undefined') return DEFAULT_PANEL_HEIGHT;
  try {
    const raw = window.localStorage.getItem(PANEL_HEIGHT_STORAGE_KEY);
    if (!raw) return DEFAULT_PANEL_HEIGHT;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < MIN_PANEL_HEIGHT) return DEFAULT_PANEL_HEIGHT;
    return n;
  } catch {
    return DEFAULT_PANEL_HEIGHT;
  }
}

const STATUS_LABEL: Record<TaskStatus, string> = {
  UNSPECIFIED: 'unspec',
  PENDING: 'pending',
  RUNNING: 'running',
  COMPLETED: 'done',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
  BLOCKED: 'blocked',
};

const STATUS_COLOR: Record<TaskStatus, string> = {
  UNSPECIFIED: '#8d9199',
  PENDING: '#8d9199',
  RUNNING: '#5b8def',
  COMPLETED: '#4caf50',
  FAILED: '#e06070',
  CANCELLED: '#8d9199',
  BLOCKED: '#f59e0b',
};

// GanttView composes the Live Activity summary panel, the Gantt canvas, the
// transport bar, and a bottom collapsible task panel. Task chips themselves
// are drawn inside the canvas renderer (renderer.ts) so the hot path stays
// canvas-only; this bottom panel is the at-a-glance summary list.
export function GanttView() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const watch = useSessionWatch(sessionId);
  const taskPlanMode = useUiStore((s) => s.taskPlanMode);
  const taskPlanVisible = useUiStore((s) => s.taskPlanVisible);
  const setTaskPlanMode = useUiStore((s) => s.setTaskPlanMode);
  const toggleTaskPlanVisible = useUiStore((s) => s.toggleTaskPlanVisible);
  const contextOverlayVisible = useUiStore((s) => s.contextOverlayVisible);
  const toggleContextOverlayVisible = useUiStore((s) => s.toggleContextOverlayVisible);
  const interventionBandsVisible = useUiStore((s) => s.interventionBandsVisible);
  const toggleInterventionBandsVisible = useUiStore(
    (s) => s.toggleInterventionBandsVisible,
  );
  const activeRenderer = useUiStore((s) => s.activeRenderer);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const selectTask = useUiStore((s) => s.selectTask);
  const selectedTaskId = useUiStore((s) => s.selectedTaskId);
  const [panelOpen, setPanelOpen] = useState(true);
  const [panelHeight, setPanelHeight] = useState<number>(() => loadStoredPanelHeight());
  const [dragging, setDragging] = useState(false);
  const [handleHovered, setHandleHovered] = useState(false);
  const dragStartRef = useRef<{ startY: number; startHeight: number } | null>(null);
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!watch?.store) return;
    // Subscribe to tasks for stage DAG refresh AND drifts/annotations so the
    // InterventionsList re-derives + the Gantt intervention bands repaint
    // as new STEERs / drift events arrive without a ListInterventions
    // round-trip per marker.
    const un = watch.store.tasks.subscribe(() => setTick((t) => t + 1));
    const unDrift = watch.store.drifts.subscribe(() => setTick((t) => t + 1));
    // InvocationCancelled markers (goldfive#251) — same role as the
    // drift subscription: re-derive the intervention list when a new
    // cancel marker lands, so the Gantt band + InterventionsList row
    // reflect the arrival without a ListInterventions round-trip.
    const unCancel = watch.store.invocationCancels.subscribe(() =>
      setTick((t) => t + 1),
    );
    const unAnn = useAnnotationStore.subscribe(() => setTick((t) => t + 1));
    return () => {
      un();
      unDrift();
      unCancel();
      unAnn();
    };
  }, [watch?.store]);

  useEffect(() => {
    try {
      window.localStorage.setItem(PANEL_HEIGHT_STORAGE_KEY, String(panelHeight));
    } catch {
      // localStorage may be unavailable (private mode, quota); ignore.
    }
  }, [panelHeight]);

  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: MouseEvent) => {
      const start = dragStartRef.current;
      if (!start) return;
      const delta = start.startY - e.clientY;
      const maxHeight = Math.max(MIN_PANEL_HEIGHT, window.innerHeight - 200);
      const next = Math.min(
        maxHeight,
        Math.max(MIN_PANEL_HEIGHT, start.startHeight + delta),
      );
      setPanelHeight(next);
    };
    const onUp = () => {
      setDragging(false);
      dragStartRef.current = null;
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    const prevCursor = document.body.style.cursor;
    const prevUserSelect = document.body.style.userSelect;
    document.body.style.cursor = 'ns-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = prevCursor;
      document.body.style.userSelect = prevUserSelect;
    };
  }, [dragging]);

  const onHandleMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    // If collapsed, expand so the drag actually grows the panel body.
    const startHeight = panelOpen ? panelHeight : DEFAULT_PANEL_HEIGHT;
    if (!panelOpen) {
      setPanelOpen(true);
      setPanelHeight(startHeight);
    }
    dragStartRef.current = { startY: e.clientY, startHeight };
    setDragging(true);
  };

  const store = watch?.store;
  const plans: readonly TaskPlan[] = store ? store.tasks.listPlans() : [];
  const totalTasks = plans.reduce((n, p) => n + p.tasks.length, 0);
  // Derive the unified intervention history from the live session store +
  // any pending/delivered annotations. Derived (not fetched) so live
  // updates do not require an extra server round-trip.
  const allAnnotations = sessionId
    ? useAnnotationStore.getState().list(sessionId)
    : [];
  const interventions = store
    ? deriveInterventionsFromStore(store, allAnnotations)
    : [];

  // Push the derived intervention rows down to the Gantt renderer so it
  // can paint translucent bands at each intervention's atMs on the
  // overlay layer. The renderer no-ops on ref-equal arrays, so this
  // effect is cheap even when the deriver produces a new array every
  // tick with identical contents.
  useEffect(() => {
    activeRenderer?.setInterventions(interventions);
  }, [activeRenderer, interventions]);

  const agentNameFor = (id: string): string =>
    store?.agents.get(id)?.name ?? id;
  // Resolve the assignee's canonical color only if the agent is registered on
  // the session — otherwise fall back to muted grey so unknown agents read
  // differently from known ones.
  const agentColorFor = (id: string): string | null =>
    store?.agents.get(id) ? colorForAgent(id) : null;
  const handleTaskClick = (task: Task): void => {
    selectTask(task.id);
    if (task.boundSpanId) selectSpan(task.boundSpanId);
  };

  const panelRealHeight = panelOpen ? panelHeight : COLLAPSED_HEIGHT;

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        minHeight: 0,
      }}
    >
      <LiveActivityPanel />
      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <GanttPlaceholder />
      </div>
      <TransportBar />
      <div
        data-testid="gantt-task-panel"
        style={{
          height: panelRealHeight,
          flexShrink: 0,
          borderTop: '1px solid var(--md-sys-color-outline-variant, #43474e)',
          background: 'var(--md-sys-color-surface-container, #1c1f26)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          transition: dragging ? 'none' : 'height 120ms ease-out',
          position: 'relative',
        }}
      >
        <div
          data-testid="gantt-task-panel-resize-handle"
          role="separator"
          aria-orientation="horizontal"
          aria-label="Resize task panel"
          onMouseDown={onHandleMouseDown}
          onMouseEnter={() => setHandleHovered(true)}
          onMouseLeave={() => setHandleHovered(false)}
          style={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            height: 5,
            cursor: 'ns-resize',
            background: 'transparent',
            zIndex: 2,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div
            aria-hidden="true"
            style={{
              width: 24,
              height: 2,
              borderRadius: 1,
              background: dragging
                ? 'var(--md-sys-color-primary, #a8c8ff)'
                : 'var(--md-sys-color-on-surface-variant, #c3c6cf)',
              opacity: dragging ? 1 : handleHovered ? 0.7 : 0.4,
              transition: 'opacity 120ms ease-out, background 120ms ease-out',
            }}
          />
        </div>
        <div
          style={{
            height: 28,
            flexShrink: 0,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '0 12px',
            fontSize: 11,
            color: 'var(--md-sys-color-on-surface-variant, #c3c6cf)',
            borderBottom: panelOpen
              ? '1px solid var(--md-sys-color-outline-variant, #43474e)'
              : 'none',
          }}
        >
          <button
            onClick={() => setPanelOpen((v) => !v)}
            data-testid="gantt-task-panel-toggle"
            aria-expanded={panelOpen}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'inherit',
              cursor: 'pointer',
              fontSize: 11,
              padding: '2px 6px',
            }}
          >
            {panelOpen ? '▾' : '▸'} Tasks
          </button>
          <span>
            {plans.length} plan{plans.length === 1 ? '' : 's'} · {totalTasks} task
            {totalTasks === 1 ? '' : 's'}
          </span>
          <span style={{ flex: 1 }} />
          <label style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <input
              type="checkbox"
              checked={taskPlanVisible}
              onChange={toggleTaskPlanVisible}
            />
            show
          </label>
          <select
            value={taskPlanMode}
            disabled={!taskPlanVisible}
            onChange={(e) => setTaskPlanMode(e.target.value as TaskPlanMode)}
            style={{
              fontSize: 11,
              background: 'var(--md-sys-color-surface-container-high, #262931)',
              color: 'inherit',
              border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
              borderRadius: 4,
              padding: '1px 4px',
            }}
          >
            <option value="pre-strip">pre-strip</option>
            <option value="ghost">ghost</option>
            <option value="hybrid">hybrid</option>
          </select>
          <label
            style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: 8 }}
            title="Show per-agent context-window usage band"
          >
            <input
              type="checkbox"
              data-testid="ctxwin-toggle"
              checked={contextOverlayVisible}
              onChange={toggleContextOverlayVisible}
            />
            context
          </label>
          <label
            style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: 8 }}
            title="Show translucent bands on the Gantt at each intervention's time"
          >
            <input
              type="checkbox"
              data-testid="intervention-bands-toggle"
              checked={interventionBandsVisible}
              onChange={toggleInterventionBandsVisible}
            />
            bands
          </label>
        </div>
        {panelOpen && (
          <div
            style={{
              flex: 1,
              minHeight: 0,
              overflow: 'auto',
              padding: '6px 12px',
              display: 'flex',
              flexDirection: 'column',
              gap: 4,
            }}
          >
            {plans.length === 0 && (
              <div
                style={{
                  fontSize: 11,
                  color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
                  fontStyle: 'italic',
                }}
              >
                No task plans in this session yet.
              </div>
            )}
            {plans.map((plan) => (
              <div
                key={`stages-${plan.id}`}
                style={{ display: 'flex', flexDirection: 'column', gap: 4 }}
              >
                <TaskPlanPanel
                  sessionId={sessionId}
                  plan={plan}
                  selectedTaskId={selectedTaskId}
                  agentColorFor={agentColorFor}
                  agentNameFor={agentNameFor}
                  onTaskClick={handleTaskClick}
                />
                <InterventionsList
                  rows={interventions.filter(
                    // Attach each intervention to the plan whose rev it
                    // produced, or — if it never produced a revision —
                    // the nearest preceding plan. Keeps every plan
                    // uncluttered while still showing every
                    // intervention exactly once.
                    (row) =>
                      row.planRevisionIndex > 0
                        ? (plan.revisionIndex ?? 0) === row.planRevisionIndex
                        : (plan.revisionIndex ?? 0) === 0,
                  )}
                />
              </div>
            ))}
            {plans.map((plan) =>
              plan.tasks.map((task) => (
                <TaskRow
                  key={`${plan.id}:${task.id}`}
                  task={task}
                  agentName={agentNameFor(task.assigneeAgentId)}
                  agentColor={agentColorFor(task.assigneeAgentId)}
                  selected={task.id === selectedTaskId}
                  onClick={() => handleTaskClick(task)}
                />
              )),
            )}
          </div>
        )}
      </div>
    </div>
  );
}

interface TaskRowProps {
  task: Task;
  agentName: string;
  agentColor: string | null;
  selected: boolean;
  onClick: () => void;
}

function TaskRow({ task, agentName, agentColor, selected, onClick }: TaskRowProps) {
  const color = STATUS_COLOR[task.status];
  const hasSpan = !!task.boundSpanId;
  const isRunning = task.status === 'RUNNING';
  // Muted grey pill when we don't know the agent yet — otherwise fill with
  // the agent's canonical color and switch to dark text for contrast.
  const pillBg = agentColor ?? 'rgba(141, 145, 153, 0.2)';
  const pillFg = agentColor ? '#0b0d12' : 'var(--md-sys-color-on-surface-variant, #9da3b4)';
  return (
    <div
      data-testid="gantt-task-row"
      data-selected={selected ? 'true' : undefined}
      onClick={onClick}
      className={isRunning ? 'hg-task-row hg-task-row--running' : 'hg-task-row'}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        fontSize: 11,
        color: 'var(--md-sys-color-on-surface, #e2e2e9)',
        cursor: 'pointer',
        lineHeight: 1.6,
        textDecoration: task.status === 'CANCELLED' ? 'line-through' : undefined,
        opacity: task.status === 'CANCELLED' ? 0.6 : 1,
        paddingLeft: 6,
        borderLeft: selected
          ? '2px solid var(--md-sys-color-primary, #a8c8ff)'
          : '2px solid transparent',
        background: selected ? 'rgba(168, 200, 255, 0.08)' : undefined,
        borderRadius: 2,
      }}
    >
      <span
        style={{
          display: 'inline-block',
          minWidth: 52,
          textAlign: 'center',
          padding: '0 6px',
          borderRadius: 10,
          fontSize: 9,
          fontWeight: 600,
          textTransform: 'uppercase',
          background: color,
          color: '#0b0d12',
        }}
      >
        {STATUS_LABEL[task.status]}
      </span>
      <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {task.title || '(untitled)'}
      </span>
      <span
        style={{
          fontSize: 10,
          padding: '0 6px',
          borderRadius: 8,
          background: pillBg,
          color: pillFg,
          whiteSpace: 'nowrap',
          fontWeight: agentColor ? 600 : 400,
        }}
      >
        {agentName}
      </span>
      {hasSpan && (
        <span
          style={{
            fontSize: 10,
            color: 'var(--md-sys-color-primary, #a8c8ff)',
            whiteSpace: 'nowrap',
          }}
        >
          → span
        </span>
      )}
    </div>
  );
}
