// TaskPlanPanel (harmonograf plan-evolution).
//
// Wraps a plan's revision scrubber + cumulative DAG + steering-detail
// side panel into a single component. Consumed by GanttView's bottom
// task panel. One <TaskPlanPanel /> per plan.
//
// The component itself is thin glue: it pulls revision history via
// state/planHistory hooks, routes scrubber state + side-panel selection
// into local React state, and hands the actual drawing to
// <TaskStagesGraph />.

import { useState } from 'react';
import type { Task, TaskPlan } from '../../gantt/types';
import {
  usePlanHistory,
  useCumulativePlan,
  useSupersedesMap,
  type SupersessionLink,
} from '../../state/planHistory';
import { useUiStore } from '../../state/uiStore';
import { TaskStagesGraph } from './TaskStagesGraph';

interface TaskPlanPanelProps {
  sessionId: string | null;
  plan: TaskPlan;
  onTaskClick?: (task: Task) => void;
  selectedTaskId?: string | null;
  agentColorFor?: (agentId: string) => string | null;
  agentNameFor?: (agentId: string) => string;
  // Optional jump-to-Gantt callback invoked when the user clicks the
  // "Jump to this drift in Gantt" button in the steering detail panel.
  onJumpToDrift?: (atMs: number) => void;
}

// Revision scrubber. Renders one notch per revision plus a "Latest" notch.
// `selected === null` means latest; any integer filters to "tasks
// introduced at-or-before this rev".
interface RevisionScrubberProps {
  revisions: ReadonlyArray<{ revisionIndex: number; revisionKind: string }>;
  selected: number | null;
  onSelect: (rev: number | null) => void;
}

function RevisionScrubber({ revisions, selected, onSelect }: RevisionScrubberProps) {
  // Keyboard-accessible: Home / End / ←/→ walk through notches.
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const order: Array<number | null> = [
      ...revisions.map((r) => r.revisionIndex),
      null,
    ];
    const idx = order.indexOf(selected);
    if (idx < 0) return;
    if (e.key === 'ArrowRight') {
      onSelect(order[Math.min(order.length - 1, idx + 1)]);
      e.preventDefault();
    } else if (e.key === 'ArrowLeft') {
      onSelect(order[Math.max(0, idx - 1)]);
      e.preventDefault();
    } else if (e.key === 'Home') {
      onSelect(order[0]);
      e.preventDefault();
    } else if (e.key === 'End') {
      onSelect(order[order.length - 1]);
      e.preventDefault();
    }
  };
  return (
    <div
      className="hg-scrubber"
      role="toolbar"
      aria-label="Plan revision scrubber"
      tabIndex={0}
      onKeyDown={onKeyDown}
      data-testid="plan-revision-scrubber"
    >
      <div className="hg-scrubber__label">Rev</div>
      <div className="hg-scrubber__track">
        {revisions.map((r) => {
          const isSel = selected === r.revisionIndex;
          const label = r.revisionKind
            ? `REV ${r.revisionIndex}: ${r.revisionKind}`
            : `REV ${r.revisionIndex}`;
          return (
            <button
              key={r.revisionIndex}
              type="button"
              className={`hg-scrubber__notch${isSel ? ' hg-scrubber__notch--selected' : ''}`}
              onClick={() => onSelect(r.revisionIndex)}
              data-testid={`scrubber-notch-${r.revisionIndex}`}
              aria-pressed={isSel}
            >
              {label}
            </button>
          );
        })}
        <button
          type="button"
          className={`hg-scrubber__notch${selected === null ? ' hg-scrubber__notch--selected' : ''}`}
          onClick={() => onSelect(null)}
          data-testid="scrubber-notch-latest"
          aria-pressed={selected === null}
        >
          Latest
        </button>
      </div>
    </div>
  );
}

// Steering-detail side panel. Opens when the user clicks a supersedes
// edge. Sections: trigger (drift reason), steering (refine_steer text),
// target (agent id). "Jump to drift in Gantt" is a best-effort scroll.
interface SteeringDetailPanelProps {
  link: SupersessionLink;
  targetAgentId: string;
  triggerAtMs: number | null;
  onClose: () => void;
  onJumpToDrift?: (atMs: number) => void;
}

function SteeringDetailPanel({
  link,
  targetAgentId,
  triggerAtMs,
  onClose,
  onJumpToDrift,
}: SteeringDetailPanelProps) {
  const authoredBy = link.authoredBy
    || (link.kind.toLowerCase().startsWith('user_') ? 'user' : 'goldfive');
  return (
    <div
      className="hg-steering-panel"
      role="dialog"
      aria-label="Steering detail"
      data-testid="steering-detail-panel"
    >
      <div className="hg-steering-panel__header">
        <span>
          Steering · rev {link.revision}
          {link.kind ? ` · ${link.kind}` : ''}
        </span>
        <button
          type="button"
          className="hg-steering-panel__close"
          aria-label="Close steering detail"
          onClick={onClose}
          data-testid="steering-detail-close"
        >
          ×
        </button>
      </div>
      <div className="hg-steering-panel__body">
        <div className="hg-steering-panel__section" data-testid="steering-detail-trigger">
          <div className="hg-steering-panel__section-label">Trigger</div>
          <div className="hg-steering-panel__section-body">
            {link.kind || '(unknown drift kind)'}
            {link.reason ? `\n\n${link.reason}` : ''}
          </div>
        </div>
        <div className="hg-steering-panel__section" data-testid="steering-detail-steering">
          <div className="hg-steering-panel__section-label">Steering</div>
          <div className="hg-steering-panel__section-body">
            {authoredBy ? `by: ${authoredBy}` : '(unknown author)'}
            {link.triggerEventId ? `\nevent: ${link.triggerEventId}` : ''}
            {link.reason ? `\n\n${link.reason}` : ''}
          </div>
        </div>
        <div className="hg-steering-panel__section" data-testid="steering-detail-target">
          <div className="hg-steering-panel__section-label">Target</div>
          <div className="hg-steering-panel__section-body">
            {targetAgentId || '(no target agent)'}
            {link.newTaskId ? `\ntask: ${link.newTaskId}` : ''}
          </div>
        </div>
        {triggerAtMs !== null && onJumpToDrift && (
          <button
            type="button"
            className="hg-steering-panel__jump"
            onClick={() => onJumpToDrift(triggerAtMs)}
            data-testid="steering-detail-jump"
          >
            Jump to this drift in Gantt
          </button>
        )}
      </div>
    </div>
  );
}

export function TaskPlanPanel({
  sessionId,
  plan,
  onTaskClick,
  selectedTaskId,
  agentColorFor,
  agentNameFor,
  onJumpToDrift,
}: TaskPlanPanelProps) {
  const history = usePlanHistory(sessionId, plan.id);
  const cumulative = useCumulativePlan(sessionId, plan.id);
  const supersedesMap = useSupersedesMap(sessionId, plan.id);
  const taskPlanView = useUiStore((s) => s.taskPlanView);
  const setTaskPlanView = useUiStore((s) => s.setTaskPlanView);
  const [scrubberSelection, setScrubberSelection] = useState<number | null>(null);
  const [openLinkId, setOpenLinkId] = useState<string | null>(null);
  // Resolve the currently-open supersedes link every render from the
  // live map so (a) it drops automatically when the map no longer
  // contains the id and (b) switching to "Latest only" trivially hides
  // the panel (see `isCumulative` gate below). Storing the id rather
  // than the record lets us stay pure — no useEffect / setState
  // cascades on prop changes.
  const openLink: SupersessionLink | null = openLinkId
    ? supersedesMap.get(openLinkId) ?? null
    : null;

  // Hide the scrubber when there's only the initial rev — nothing to scrub.
  const showScrubber = taskPlanView === 'cumulative' && history.length > 1;

  // The "Latest only" path bypasses the cumulative renderer entirely;
  // the old behaviour is preserved — see TaskStagesGraphProps legacy signature.
  const isCumulative = taskPlanView === 'cumulative' && cumulative !== null;

  // Resolve the target agent from the replacement task, falling back to
  // the drift's agent id via the supersedesMap entry.
  const resolveTargetAgent = (link: SupersessionLink): string => {
    if (link.newTaskId && cumulative) {
      for (const t of cumulative.tasks) {
        if (t.id === link.newTaskId) return t.assigneeAgentId;
      }
    }
    return '';
  };

  const triggerAtMsFor = (link: SupersessionLink): number | null => {
    const rev = history.find((r) => r.revisionIndex === link.revision);
    return rev ? rev.revisedAtMs : null;
  };

  return (
    <div
      style={{ display: 'flex', flexDirection: 'column', position: 'relative' }}
      data-testid="task-plan-panel"
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '2px 10px',
          fontSize: 10,
          color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
          textTransform: 'uppercase',
          letterSpacing: 0.5,
        }}
      >
        <span style={{ flex: 1 }}>
          Plan: {plan.summary || plan.id}
          {isCumulative && history.length > 0
            ? ` · ${history.length} rev${history.length === 1 ? '' : 's'}`
            : ''}
        </span>
        <label
          style={{ display: 'flex', alignItems: 'center', gap: 4 }}
          title="Cumulative shows all revisions with grey-muted superseded tasks; Latest shows only the current plan."
        >
          <span style={{ textTransform: 'none' }}>Show:</span>
          <select
            data-testid="task-plan-view-toggle"
            value={taskPlanView}
            onChange={(e) =>
              setTaskPlanView(e.target.value as 'cumulative' | 'latest')
            }
            style={{
              fontSize: 10,
              background: 'var(--md-sys-color-surface-container-high, #262931)',
              color: 'inherit',
              border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
              borderRadius: 4,
              padding: '1px 4px',
              textTransform: 'none',
            }}
          >
            <option value="cumulative">Cumulative</option>
            <option value="latest">Latest only</option>
          </select>
        </label>
      </div>
      {showScrubber && (
        <RevisionScrubber
          revisions={history.map((h) => ({
            revisionIndex: h.revisionIndex,
            revisionKind: h.revisionKind,
          }))}
          selected={scrubberSelection}
          onSelect={setScrubberSelection}
        />
      )}
      <TaskStagesGraph
        plan={plan}
        cumulative={isCumulative ? cumulative : null}
        supersedesMap={isCumulative ? supersedesMap : undefined}
        revisionFilter={isCumulative ? scrubberSelection : null}
        revisionFilterMode="mute"
        selectedTaskId={selectedTaskId}
        agentColorFor={agentColorFor}
        agentNameFor={agentNameFor}
        onTaskClick={onTaskClick}
        onSupersedesEdgeClick={
          isCumulative ? (link) => setOpenLinkId(link.oldTaskId) : undefined
        }
      />
      {isCumulative && openLink && (
        <SteeringDetailPanel
          link={openLink}
          targetAgentId={resolveTargetAgent(openLink)}
          triggerAtMs={triggerAtMsFor(openLink)}
          onClose={() => setOpenLinkId(null)}
          onJumpToDrift={onJumpToDrift}
        />
      )}
    </div>
  );
}
