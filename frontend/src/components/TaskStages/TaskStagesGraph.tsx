import { useMemo } from 'react';
import type { Task, TaskEdge, TaskPlan } from '../../gantt/types';
import { computeStages } from '../../gantt/stages';
import type {
  CumulativePlan,
  SupersessionLink,
  TaskRevisionMeta,
} from '../../state/planHistory';
import './TaskStagesGraph.css';

interface TaskStagesGraphProps {
  // Legacy single-plan path — still used by "Latest only" mode and by
  // pre-existing tests. When `cumulative` is passed we ignore this.
  plan: TaskPlan;
  // Cumulative-DAG path (harmonograf plan-evolution). When present the
  // renderer draws every task across every revision, grey-muting the
  // ones whose `isSuperseded === true`, and paints dashed "supersedes"
  // edges between old and new tasks.
  cumulative?: CumulativePlan | null;
  supersedesMap?: Map<string, SupersessionLink>;
  // Revision scrubber: when set to a non-null integer, tasks whose
  // `introducedInRevision > revisionFilter` are muted (or hidden, see
  // `revisionFilterMode`). Passing null = "Latest" (no filter).
  revisionFilter?: number | null;
  revisionFilterMode?: 'mute' | 'hide';
  onTaskClick?: (task: Task) => void;
  onSupersedesEdgeClick?: (link: SupersessionLink) => void;
  selectedTaskId?: string | null;
  agentColorFor?: (agentId: string) => string | null;
  agentNameFor?: (agentId: string) => string;
}

// Status chip colors — mirror the TaskChipIcon variants used in GanttLegend.
const STATUS_DOT: Record<Task['status'], string> = {
  UNSPECIFIED: '#8d9199',
  PENDING: '#8d9199',
  RUNNING: '#5b8def',
  COMPLETED: '#4caf50',
  FAILED: '#e06070',
  CANCELLED: '#8d9199',
  BLOCKED: '#f59e0b',
};

// Generation palette. Monochromatic (muted indigo → brighter) so it
// doesn't stomp on the status colours. Rev 0 is a neutral grey badge; every
// subsequent rev shifts toward the primary. Capped at 4 steps — rev 3+
// share the strongest colour so very long histories still render.
const GENERATION_PALETTE = [
  { fill: '#3a3d46', text: '#c3c6cf' }, // rev 0 (neutral)
  { fill: '#4b5770', text: '#dbe3ff' }, // rev 1
  { fill: '#5b6aa0', text: '#e9ecff' }, // rev 2
  { fill: '#7587d4', text: '#f1f3ff' }, // rev 3+
];

function generationColor(rev: number): { fill: string; text: string } {
  if (rev <= 0) return GENERATION_PALETTE[0];
  return GENERATION_PALETTE[Math.min(rev, GENERATION_PALETTE.length - 1)];
}

// Drift-kind → edge colour lookup. Autonomous goldfive kinds stay in a
// warm hue; user-origin kinds sit in a cooler hue. Unknown kinds fall
// back to a neutral outline.
function edgeKindColor(kind: string): string {
  const k = (kind || '').toLowerCase();
  if (k.startsWith('user_')) return '#8bd17c';          // cool green (user)
  if (k === 'off_topic' || k === 'off-topic') return '#f0a860';
  if (k === 'missing_work' || k === 'missing-work') return '#e76c6c';
  if (k === 'cascade_cancel') return '#c08cd6';
  if (k) return '#d8a468';                              // other goldfive
  return '#8d9199';                                      // unknown
}

function edgeAnnotation(link: SupersessionLink): string {
  const kind = link.kind || '';
  if (!kind) return 'superseded';
  const author = link.authoredBy || (kind.toLowerCase().startsWith('user_') ? 'user' : 'goldfive');
  return `${author}: ${kind.toLowerCase()}`;
}

const COLUMN_WIDTH = 140;
const CARD_WIDTH = 120;
const CARD_HEIGHT = 38;
const CARD_GAP = 10;
const TOP_PADDING = 28; // for stage labels + progress badges
const COL_PADDING = 10;
const SIDE_PADDING = 12;
const AGENT_STRIPE_W = 4;

interface LaidOutCard {
  task: Task;
  x: number;
  y: number;
  stage: number;
  hidden: boolean;   // true when filtered out by scrubber (hide mode)
  muted: boolean;    // superseded OR scrubber-filtered (mute mode)
}

// Synthesize a TaskPlan shape from a CumulativePlan so we can reuse
// computeStages() directly. The returned plan is purely for layout —
// no other code reads it.
function cumulativeAsPlan(cum: CumulativePlan): TaskPlan {
  return {
    id: cum.planId,
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks: cum.tasks,
    edges: cum.edges,
    revisionReason: '',
  };
}

export function TaskStagesGraph({
  plan,
  cumulative,
  supersedesMap,
  revisionFilter = null,
  revisionFilterMode = 'mute',
  onTaskClick,
  onSupersedesEdgeClick,
  selectedTaskId = null,
  agentColorFor,
  agentNameFor,
}: TaskStagesGraphProps) {
  const layout = useMemo(() => {
    const sourcePlan: TaskPlan = cumulative ? cumulativeAsPlan(cumulative) : plan;
    const stages = computeStages(sourcePlan);
    if (stages.length === 0) return null;

    const revMeta: Map<string, TaskRevisionMeta> | null = cumulative
      ? cumulative.taskRevisionMeta
      : null;

    const cards = new Map<string, LaidOutCard>();
    let maxRows = 0;
    stages.forEach((stageTasks, stageIdx) => {
      if (stageTasks.length > maxRows) maxRows = stageTasks.length;
      stageTasks.forEach((task, rowIdx) => {
        const x = SIDE_PADDING + stageIdx * COLUMN_WIDTH + COL_PADDING;
        const y = TOP_PADDING + rowIdx * (CARD_HEIGHT + CARD_GAP);
        const meta = revMeta?.get(task.id);
        let hidden = false;
        let muted = Boolean(meta?.isSuperseded);
        if (revisionFilter !== null && meta) {
          const introduced = meta.introducedInRevision;
          if (introduced > revisionFilter) {
            if (revisionFilterMode === 'hide') hidden = true;
            else muted = true;
          }
        }
        cards.set(task.id, { task, x, y, stage: stageIdx, hidden, muted });
      });
    });

    const width = SIDE_PADDING * 2 + stages.length * COLUMN_WIDTH;
    const contentHeight =
      TOP_PADDING + maxRows * (CARD_HEIGHT + CARD_GAP) + 8;
    const height = Math.max(contentHeight, 116);

    // Plan-DAG edges (solid). Only keep forward edges — same rule as before.
    const planEdges: Array<{ from: LaidOutCard; to: LaidOutCard }> = [];
    const edgeSource: ReadonlyArray<TaskEdge> = sourcePlan.edges;
    for (const e of edgeSource) {
      const from = cards.get(e.fromTaskId);
      const to = cards.get(e.toTaskId);
      if (!from || !to) continue;
      if (from.hidden || to.hidden) continue;
      if (to.stage <= from.stage) continue;
      planEdges.push({ from, to });
    }

    // Supersedes edges (dashed, one per supersession link).
    const supersedesEdges: Array<{
      from: LaidOutCard;
      to: LaidOutCard;
      link: SupersessionLink;
    }> = [];
    if (supersedesMap) {
      for (const link of supersedesMap.values()) {
        const from = cards.get(link.oldTaskId);
        const to = link.newTaskId ? cards.get(link.newTaskId) : undefined;
        if (!from || !to) continue;
        if (from.hidden || to.hidden) continue;
        supersedesEdges.push({ from, to, link });
      }
    }

    return { stages, cards, width, height, planEdges, supersedesEdges };
  }, [plan, cumulative, supersedesMap, revisionFilter, revisionFilterMode]);

  if (!layout) return null;
  const { stages, cards, width, height, planEdges, supersedesEdges } = layout;
  const revMeta = cumulative?.taskRevisionMeta;

  return (
    <div className="hg-stages" data-testid="task-stages-graph">
      <svg
        className="hg-stages__svg"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
      >
        <defs>
          <marker
            id="hg-stages-arrow"
            viewBox="0 0 10 10"
            refX={9}
            refY={5}
            markerWidth={6}
            markerHeight={6}
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--md-sys-color-outline, #8d9199)" />
          </marker>
          <marker
            id="hg-stages-supersedes-arrow"
            viewBox="0 0 10 10"
            refX={9}
            refY={5}
            markerWidth={6}
            markerHeight={6}
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#d8a468" />
          </marker>
        </defs>

        {stages.map((_, stageIdx) => {
          if (stageIdx === 0) return null;
          const x = SIDE_PADDING + stageIdx * COLUMN_WIDTH;
          return (
            <line
              key={`rule-${stageIdx}`}
              className="hg-stages__column-rule"
              x1={x}
              y1={4}
              x2={x}
              y2={height - 4}
            />
          );
        })}

        {stages.map((stageTasks, stageIdx) => {
          const x = SIDE_PADDING + stageIdx * COLUMN_WIDTH + COLUMN_WIDTH / 2;
          // Progress badge counts only NON-superseded tasks so the N/M
          // reflects the live plan, not historical noise.
          const visible = stageTasks.filter((t) => {
            const m = revMeta?.get(t.id);
            return !m || !m.isSuperseded;
          });
          const total = visible.length;
          const done = visible.filter((t) => t.status === 'COMPLETED').length;
          const badgeClass =
            total === 0
              ? 'hg-stages__progress hg-stages__progress--none'
              : done === 0
                ? 'hg-stages__progress hg-stages__progress--none'
                : done === total
                  ? 'hg-stages__progress hg-stages__progress--done'
                  : 'hg-stages__progress hg-stages__progress--partial';
          return (
            <g key={`label-${stageIdx}`}>
              <text
                className="hg-stages__stage-label"
                x={x - 14}
                y={14}
                textAnchor="middle"
              >
                Stage {stageIdx}
              </text>
              <g transform={`translate(${x + 22}, 6)`}>
                <rect
                  className={badgeClass}
                  x={-13}
                  y={-1}
                  width={26}
                  height={12}
                  rx={6}
                  ry={6}
                />
                <text
                  className="hg-stages__progress-text"
                  x={0}
                  y={8}
                  textAnchor="middle"
                >
                  {done}/{total}
                </text>
              </g>
            </g>
          );
        })}

        {planEdges.map(({ from, to }, i) => {
          const x1 = from.x + CARD_WIDTH;
          const y1 = from.y + CARD_HEIGHT / 2;
          const x2 = to.x;
          const y2 = to.y + CARD_HEIGHT / 2;
          const dx = Math.max(30, (x2 - x1) * 0.5);
          const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
          return (
            <path
              key={`edge-${from.task.id}-${to.task.id}-${i}`}
              className="hg-stages__edge"
              d={d}
              markerEnd="url(#hg-stages-arrow)"
            />
          );
        })}

        {supersedesEdges.map(({ from, to, link }, i) => {
          const x1 = from.x + CARD_WIDTH;
          const y1 = from.y + CARD_HEIGHT / 2;
          const x2 = to.x;
          const y2 = to.y + CARD_HEIGHT / 2;
          const dx = Math.max(30, (x2 - x1) * 0.5);
          const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
          const color = edgeKindColor(link.kind);
          const label = edgeAnnotation(link);
          // Label anchors near the midpoint of the edge. Offset by -6 so
          // the annotation text sits above the curve.
          const mx = (x1 + x2) / 2;
          const my = (y1 + y2) / 2 - 6;
          return (
            <g
              key={`sedge-${link.oldTaskId}-${link.newTaskId}-${i}`}
              className="hg-stages__supersedes"
              data-testid="supersedes-edge"
              data-kind={link.kind || ''}
              onClick={(e) => {
                e.stopPropagation();
                onSupersedesEdgeClick?.(link);
              }}
              style={{ cursor: onSupersedesEdgeClick ? 'pointer' : 'default' }}
            >
              <title>{link.reason || link.kind || 'superseded'}</title>
              <path
                className="hg-stages__supersedes-path"
                d={d}
                stroke={color}
                markerEnd="url(#hg-stages-supersedes-arrow)"
              />
              <text
                className="hg-stages__supersedes-label"
                x={mx}
                y={my}
                textAnchor="middle"
                fill={color}
              >
                {label}
              </text>
            </g>
          );
        })}

        {Array.from(cards.values()).map(({ task, x, y, muted, hidden }) => {
          if (hidden) return null;
          const dotColor = STATUS_DOT[task.status];
          const title = task.title || '(untitled)';
          const maxChars = 14;
          const truncated =
            title.length > maxChars ? title.slice(0, maxChars - 1) + '…' : title;
          const selected = task.id === selectedTaskId;
          const running = task.status === 'RUNNING';
          const agentColor = agentColorFor?.(task.assigneeAgentId) ?? null;
          const rawAgentName = agentNameFor?.(task.assigneeAgentId) ?? '';
          const agentLabel =
            rawAgentName.length > 16
              ? rawAgentName.slice(0, 15) + '…'
              : rawAgentName;
          const meta = revMeta?.get(task.id);
          const classes = [
            'hg-stages__card',
            selected ? 'hg-stages__card--selected' : '',
            running ? 'hg-stages__card--running' : '',
            muted ? 'hg-stages__card--muted' : '',
            meta?.isSuperseded ? 'hg-stages__card--superseded' : '',
          ]
            .filter(Boolean)
            .join(' ');
          const reason = task.cancelReason || '';
          const showReason =
            reason && (task.status === 'CANCELLED' || task.status === 'FAILED');
          const tooltipText = showReason ? `${title}\n${reason}` : title;
          const badgeRev = meta?.lastModifiedInRevision ?? 0;
          const { fill: badgeFill, text: badgeTextColor } = generationColor(badgeRev);
          return (
            <g
              key={task.id}
              className={classes}
              transform={`translate(${x}, ${y})`}
              onClick={() => onTaskClick?.(task)}
              data-superseded={meta?.isSuperseded ? 'true' : 'false'}
            >
              <title>{tooltipText}</title>
              <rect
                className="hg-stages__card-rect"
                width={CARD_WIDTH}
                height={CARD_HEIGHT}
                rx={6}
                ry={6}
              />
              {/* Agent color stripe on the left edge so the assignee reads at
                  a glance even when the agent label doesn't fit. */}
              <rect
                x={0}
                y={0}
                width={AGENT_STRIPE_W}
                height={CARD_HEIGHT}
                rx={2}
                ry={2}
                fill={agentColor ?? 'var(--md-sys-color-outline-variant, #43474e)'}
              />
              <circle cx={AGENT_STRIPE_W + 10} cy={12} r={4} fill={dotColor} />
              <text className="hg-stages__card-title" x={AGENT_STRIPE_W + 20} y={15}>
                {truncated}
              </text>
              {agentLabel && (
                <text
                  className="hg-stages__card-agent"
                  x={AGENT_STRIPE_W + 8}
                  y={28}
                >
                  {agentLabel}
                </text>
              )}
              {/* Generation badge: only rendered in cumulative mode. */}
              {revMeta && (
                <g
                  className="hg-stages__gen-badge"
                  data-testid="gen-badge"
                  data-rev={badgeRev}
                  transform={`translate(${CARD_WIDTH - 4}, 4)`}
                >
                  <rect
                    x={-28}
                    y={0}
                    width={28}
                    height={12}
                    rx={3}
                    ry={3}
                    fill={badgeFill}
                  />
                  <text
                    className="hg-stages__gen-badge-text"
                    x={-14}
                    y={9}
                    textAnchor="middle"
                    fill={badgeTextColor}
                  >
                    REV {badgeRev}
                  </text>
                </g>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
