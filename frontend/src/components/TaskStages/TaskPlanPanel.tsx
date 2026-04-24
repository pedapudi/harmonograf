// TaskPlanPanel (harmonograf plan-evolution).
//
// Wraps a plan's cumulative DAG + steering-detail side panel into a single
// component. Consumed by GanttView's bottom task panel. One <TaskPlanPanel />
// per plan.
//
// Rev selection: this panel is a READ-ONLY mirror of the Trajectory view's
// `selectedRevision` slice on `useUiStore`. There is no local scrubber — if
// the user wants to pick a different revision they do it from the Trajectory
// view's ribbon / scrubber and both surfaces update. Consolidated per the
// plan-view redesign so there's a single source of truth.
//
// The component itself is thin glue: it pulls cumulative + supersedes + rev
// history via state/planHistory hooks, reads the shared `selectedRevision`,
// and hands the actual drawing to <TaskStagesGraph />.

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

// Plan-summary block. Mixed-case body wrapping to up to 3 lines with an
// inline "show more" expander when the text overflows. Lead-in "Plan" is
// rendered as an unobtrusive label. Per the redesign we no longer shout
// the summary in ALL CAPS on a single truncated line.
interface PlanSummaryProps {
  summary: string;
}

// CSS -webkit-line-clamp driven, so the 3-line cap is a rendering
// invariant rather than a character count. The expander becomes visible
// when the scrollHeight exceeds the clamped client height.
function PlanSummary({ summary }: PlanSummaryProps) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const bodyRef = (node: HTMLDivElement | null): void => {
    if (!node) return;
    // scrollHeight > clientHeight ⇒ the clamp is actually truncating.
    // Run synchronously in the ref so layout data is fresh per render.
    const hasOverflow = node.scrollHeight > node.clientHeight + 1;
    if (hasOverflow !== overflows) setOverflows(hasOverflow);
  };
  return (
    <div
      data-testid="plan-summary"
      style={{ display: 'flex', flexDirection: 'column', gap: 2 }}
    >
      <div
        style={{
          fontSize: 9,
          letterSpacing: 0.6,
          textTransform: 'uppercase',
          color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
          fontWeight: 600,
        }}
      >
        Plan
      </div>
      <div
        ref={bodyRef}
        data-testid="plan-summary-body"
        data-expanded={expanded ? 'true' : 'false'}
        style={{
          fontSize: 11,
          lineHeight: 1.4,
          color: 'var(--md-sys-color-on-surface, #e2e2e9)',
          // Mixed case — textTransform stays at its default ('none').
          whiteSpace: 'normal',
          overflow: 'hidden',
          display: expanded ? 'block' : '-webkit-box',
          WebkitLineClamp: expanded ? 'unset' : 3,
          WebkitBoxOrient: 'vertical',
        }}
      >
        {summary}
      </div>
      {overflows && (
        <button
          type="button"
          data-testid="plan-summary-toggle"
          onClick={() => setExpanded((v) => !v)}
          style={{
            alignSelf: 'flex-start',
            background: 'transparent',
            border: 'none',
            padding: 0,
            fontSize: 10,
            color: 'var(--md-sys-color-primary, #a8c8ff)',
            cursor: 'pointer',
          }}
        >
          {expanded ? 'show less' : 'show more'}
        </button>
      )}
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
  // Shared with TrajectoryView. Read-only from this panel's perspective —
  // the Trajectory ribbon/scrubber is the single authoring surface.
  const selectedRevision = useUiStore(
    (s) => (s as { selectedRevision?: number | null }).selectedRevision ?? null,
  );
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

  // The "Latest only" path bypasses the cumulative renderer entirely;
  // the old behaviour is preserved — see TaskStagesGraphProps legacy signature.
  const isCumulative = taskPlanView === 'cumulative' && cumulative !== null;

  // Inline hint text. Shown only when the panel has >1 rev to speak of
  // (otherwise there's nothing for the Trajectory view to drive) and
  // only in cumulative mode. Mirrors the shared `selectedRevision`.
  const syncHint =
    isCumulative && history.length > 1
      ? selectedRevision === null
        ? 'Showing Latest (synced with Trajectory view)'
        : `Showing REV ${selectedRevision} (synced with Trajectory view)`
      : null;

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

  const summaryText = plan.summary || plan.id;

  return (
    <div
      style={{ display: 'flex', flexDirection: 'column', position: 'relative' }}
      data-testid="task-plan-panel"
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 8,
          padding: '4px 10px',
          color: 'var(--md-sys-color-on-surface-variant, #c3c6cf)',
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <PlanSummary summary={summaryText} />
        </div>
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            fontSize: 10,
            color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
            flexShrink: 0,
            marginTop: 2,
          }}
          title="Cumulative shows all revisions as a union DAG; Latest shows only the current plan."
        >
          <span>Show:</span>
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
            }}
          >
            <option value="cumulative">Cumulative</option>
            <option value="latest">Latest only</option>
          </select>
        </label>
      </div>
      {syncHint && (
        <div
          data-testid="plan-sync-hint"
          style={{
            fontSize: 10,
            padding: '0 10px 4px',
            color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
            fontStyle: 'italic',
          }}
        >
          {syncHint}
        </div>
      )}
      <TaskStagesGraph
        plan={plan}
        cumulative={isCumulative ? cumulative : null}
        supersedesMap={isCumulative ? supersedesMap : undefined}
        revisionFilter={isCumulative ? selectedRevision : null}
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
