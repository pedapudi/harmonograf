import { useMemo } from 'react';
import type { Task, TaskEdge, TaskPlan } from '../../gantt/types';
import { computeStages } from '../../gantt/stages';
import type { CumulativePlan as StoreCumulativePlan } from '../../state/planHistoryStore';
import {
  collapseCumulativePlan,
  filterCollapsedAtRevision,
  type CollapsedCumulativePlan,
  type TaskRevisionChain,
} from './collapsedLayout';
import { RevisionHistoryBadge } from './RevisionHistoryBadge';
import './TaskStagesGraph.css';

/** Structural shape consumed by this component. Matches both
 *  `state/planHistory.CumulativePlan` (legacy, uses `planId`) and
 *  `state/planHistoryStore.CumulativePlan` (extends TaskPlan, uses
 *  `id`). We accept either and normalise into the store shape before
 *  handing off to `collapseCumulativePlan`. */
export interface CumulativePlanInput {
  /** Legacy id field (planHistory.ts). */
  planId?: string;
  /** TaskPlan-inherited id field (planHistoryStore.ts). */
  id?: string;
  tasks: Task[];
  edges: TaskEdge[];
  taskRevisionMeta: Map<
    string,
    { introducedInRevision: number; lastModifiedInRevision?: number; isSuperseded: boolean }
  >;
}

/** Minimal SupersessionLink shape consumed by collapseCumulativePlan.
 *  Both producers supply these; the extra fields the store/legacy
 *  types carry (authoredBy, reason, kind) are unused by the collapse
 *  pass and safely ignored. */
export interface SupersessionLinkInput {
  oldTaskId: string;
  newTaskId: string;
  revision: number;
  kind: string;
  reason: string;
  triggerEventId: string;
}

/** Convert a `CumulativePlanInput` into the strict store shape the
 *  collapse pass expects. Copies arrays/maps shallowly — the caller
 *  retains ownership of the originals. */
function adaptCumulative(input: CumulativePlanInput): StoreCumulativePlan {
  const id = input.id ?? input.planId ?? '';
  const meta = new Map<
    string,
    { introducedInRevision: number; lastModifiedInRevision: number; isSuperseded: boolean }
  >();
  for (const [k, v] of input.taskRevisionMeta) {
    meta.set(k, {
      introducedInRevision: v.introducedInRevision,
      lastModifiedInRevision:
        v.lastModifiedInRevision ?? v.introducedInRevision,
      isSuperseded: v.isSuperseded,
    });
  }
  return {
    id,
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks: input.tasks,
    edges: input.edges,
    revisionReason: '',
    taskRevisionMeta: meta,
  };
}

interface TaskStagesGraphProps {
  // Legacy single-plan path — still used by "Latest only" mode and by
  // pre-existing tests. When `cumulative` is passed we ignore this.
  plan: TaskPlan;
  // Cumulative-DAG path (harmonograf plan-evolution). When present the
  // renderer collapses supersedes chains into single positional slots:
  // one canonical card per chain, with a RevisionHistoryBadge surfacing
  // the predecessor trail inline on the card.
  cumulative?: CumulativePlanInput | null;
  supersedesMap?: Map<string, SupersessionLinkInput>;
  // Revision pin: when set to a non-null integer, chains whose canonical
  // was introduced after `revisionFilter` are HIDDEN entirely (not muted
  // or dashed). Null = "Latest" (no filter). The old 'mute' mode has
  // been retired with the plan-view redesign — future-rev tasks no
  // longer ghost onto the canvas.
  revisionFilter?: number | null;
  onTaskClick?: (task: Task) => void;
  // Retained for signature compatibility with TaskPlanPanel and legacy
  // call sites. After the chain-collapse refactor the renderer no longer
  // paints visible supersedes edges — the RevisionHistoryBadge's
  // onClickMember surfaces predecessor detail instead. This prop is
  // therefore unreachable from this view and will not fire; it's kept so
  // existing callers don't need a simultaneous update.
  onSupersedesEdgeClick?: (link: SupersessionLinkInput) => void;
  selectedTaskId?: string | null;
  agentColorFor?: (agentId: string) => string | null;
  agentNameFor?: (agentId: string) => string;
}

// Status → card fill color. Two orthogonal axes with the corner chip:
//   * Card fill = execution status.
//   * Corner chip = rev provenance.
// No cross-axis encoding; no dashed borders, no opacity tricks.
const STATUS_FILL: Record<Task['status'], string> = {
  UNSPECIFIED: '#2a2d35',
  PENDING: '#2a2d35',
  RUNNING: '#223554',       // accent-tinted; animated border adds the pulse
  COMPLETED: '#1f3b29',     // green-tinted
  FAILED: '#3f1f27',        // red-tinted
  CANCELLED: '#3a2a20',     // amber-tinted (a touch warmer than BLOCKED)
  BLOCKED: '#3a2f1f',       // amber-tinted
};

// Status dot. Kept so the status is legible at a glance even when fill
// contrast is low (the fill is a muted tint, the dot is saturated).
const STATUS_DOT: Record<Task['status'], string> = {
  UNSPECIFIED: '#8d9199',
  PENDING: '#8d9199',
  RUNNING: '#5b8def',
  COMPLETED: '#4caf50',
  FAILED: '#e06070',
  CANCELLED: '#8d9199',
  BLOCKED: '#f59e0b',
};

const COLUMN_WIDTH = 140;
const CARD_WIDTH = 120;
const CARD_HEIGHT = 38;
const CARD_GAP = 10;
const TOP_PADDING = 24; // for stage labels
const COL_PADDING = 10;
const SIDE_PADDING = 12;
const AGENT_STRIPE_W = 4;

interface LaidOutCard {
  task: Task;
  /** Chain this card represents. Always non-null. For non-cumulative
   *  ("latest-only") mode or for singleton tasks, a synthetic 1-member
   *  chain is created so downstream rendering is uniform. */
  chain: TaskRevisionChain;
  x: number;
  y: number;
  stage: number;
}

// Synthesize a TaskPlan shape from a task + edge list so we can reuse
// computeStages() directly. The returned plan is purely for layout —
// no other code reads it.
function asLayoutPlan(
  id: string,
  tasks: readonly Task[],
  edges: readonly TaskEdge[],
): TaskPlan {
  return {
    id,
    invocationSpanId: '',
    plannerAgentId: '',
    createdAtMs: 0,
    summary: '',
    tasks: [...tasks],
    edges: [...edges],
    revisionReason: '',
  };
}

/** Wrap a legacy per-task plan (non-cumulative) into synthetic singleton
 *  chains so downstream rendering can use the same `LaidOutCard.chain`
 *  structure. Revision is 0 for every task; no predecessors. */
function singletonChainFor(task: Task): TaskRevisionChain {
  return {
    canonical: task,
    members: [task],
    revisions: [0],
  };
}

export function TaskStagesGraph({
  plan,
  cumulative,
  supersedesMap,
  revisionFilter = null,
  onTaskClick,
  // `onSupersedesEdgeClick` kept in the prop signature for backward
  // compatibility; never fires from this renderer (see prop docstring).
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  onSupersedesEdgeClick: _onSupersedesEdgeClick,
  selectedTaskId = null,
  agentColorFor,
  agentNameFor,
}: TaskStagesGraphProps) {
  const layout = useMemo(() => {
    // Build the source task/edge list the layout works on.
    //
    // Cumulative mode with a supersedesMap → collapse the plan into one
    // canonical task per chain, rewrite edges to anchor on canonicals,
    // and carry each card's chain so the RevisionHistoryBadge can
    // surface the predecessor trail.
    //
    // Non-cumulative (legacy) mode → one card per task, synthetic
    // singleton chains so the badge path is a uniform no-op.
    let sourceTasks: readonly Task[];
    let sourceEdges: readonly TaskEdge[];
    let chainForTaskId: Map<string, TaskRevisionChain>;
    let collapsed: CollapsedCumulativePlan | null = null;
    let hiddenChainIds: ReadonlySet<string> = new Set();

    if (cumulative && supersedesMap) {
      const storeShape = adaptCumulative(cumulative);
      // `supersedesMap` from the legacy producer carries an extra
      // `authoredBy` field the store shape doesn't — the collapse pass
      // ignores it, so a structural cast is safe here.
      collapsed = collapseCumulativePlan(
        storeShape,
        supersedesMap as unknown as Map<
          string,
          import('../../state/planHistoryStore').SupersessionLink
        >,
      );
      const filtered = filterCollapsedAtRevision(collapsed, revisionFilter);
      // Per the redesign, future-rev tasks are HIDDEN entirely, not
      // ghost-dashed. Treat any chain `filterCollapsedAtRevision`
      // flagged as muted (root born ≤ pin but canonical from a later
      // rev) as hidden too — the panel should read as "tasks that
      // existed at this rev", period.
      hiddenChainIds = new Set([
        ...filtered.hiddenChainIds,
        ...filtered.mutedChainIds,
      ]);
      sourceTasks = collapsed.chains.map((c) => c.canonical);
      sourceEdges = collapsed.edges;
      chainForTaskId = new Map();
      for (const c of collapsed.chains) {
        chainForTaskId.set(c.canonical.id, c);
      }
    } else {
      sourceTasks = plan.tasks;
      sourceEdges = plan.edges;
      chainForTaskId = new Map();
      for (const t of plan.tasks) {
        chainForTaskId.set(t.id, singletonChainFor(t));
      }
    }

    // Filter OUT hidden chains from the layout input entirely so stage
    // buckets don't reserve empty slots for them.
    const visibleTasks = sourceTasks.filter(
      (t) => !hiddenChainIds.has(t.id),
    );
    const visibleEdges = sourceEdges.filter(
      (e) =>
        !hiddenChainIds.has(e.fromTaskId) && !hiddenChainIds.has(e.toTaskId),
    );

    // `cumulative` may be the legacy shape (`planId`) or the store
    // shape (`id`); fall back to the incoming plan id.
    const sourcePlanId = cumulative?.id ?? cumulative?.planId ?? plan.id;
    const sourcePlan = asLayoutPlan(sourcePlanId, visibleTasks, visibleEdges);
    const stages = computeStages(sourcePlan);
    if (stages.length === 0) return null;

    const revMeta = cumulative?.taskRevisionMeta ?? null;

    const cards = new Map<string, LaidOutCard>();
    let maxRows = 0;
    stages.forEach((stageTasks, stageIdx) => {
      if (stageTasks.length > maxRows) maxRows = stageTasks.length;
      stageTasks.forEach((task, rowIdx) => {
        const x = SIDE_PADDING + stageIdx * COLUMN_WIDTH + COL_PADDING;
        const y = TOP_PADDING + rowIdx * (CARD_HEIGHT + CARD_GAP);
        const chain =
          chainForTaskId.get(task.id) ?? singletonChainFor(task);
        cards.set(task.id, {
          task,
          chain,
          x,
          y,
          stage: stageIdx,
        });
      });
    });

    const width = SIDE_PADDING * 2 + stages.length * COLUMN_WIDTH;
    const contentHeight =
      TOP_PADDING + maxRows * (CARD_HEIGHT + CARD_GAP) + 8;
    const height = Math.max(contentHeight, 116);

    // Plan-DAG edges (solid). Only keep forward edges — same rule as before.
    const planEdges: Array<{ from: LaidOutCard; to: LaidOutCard }> = [];
    for (const e of visibleEdges) {
      const from = cards.get(e.fromTaskId);
      const to = cards.get(e.toTaskId);
      if (!from || !to) continue;
      if (to.stage <= from.stage) continue;
      planEdges.push({ from, to });
    }

    return {
      stages,
      cards,
      width,
      height,
      planEdges,
      revMeta,
      collapsed,
    };
  }, [plan, cumulative, supersedesMap, revisionFilter]);

  if (!layout) return null;
  const { stages, cards, width, height, planEdges, revMeta } = layout;

  const handleBadgeMemberClick = (member: Task) => {
    onTaskClick?.(member);
  };

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

        {/* Stage labels. Counter dropped — card fill already signals
            progress, and "0/1" was ambiguous about unit (tasks? cards?). */}
        {stages.map((_, stageIdx) => {
          const x = SIDE_PADDING + stageIdx * COLUMN_WIDTH + COLUMN_WIDTH / 2;
          return (
            <text
              key={`label-${stageIdx}`}
              className="hg-stages__stage-label"
              x={x}
              y={14}
              textAnchor="middle"
            >
              Stage {stageIdx}
            </text>
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

        {Array.from(cards.values()).map(({ task, x, y }) => {
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
          const statusFill = STATUS_FILL[task.status];
          const dotColor = STATUS_DOT[task.status];
          const classes = [
            'hg-stages__card',
            selected ? 'hg-stages__card--selected' : '',
            running ? 'hg-stages__card--running' : '',
          ]
            .filter(Boolean)
            .join(' ');
          const reason = task.cancelReason || '';
          const showReason =
            reason && (task.status === 'CANCELLED' || task.status === 'FAILED');
          const tooltipText = showReason ? `${title}\n${reason}` : title;
          return (
            <g
              key={task.id}
              className={classes}
              transform={`translate(${x}, ${y})`}
              onClick={() => onTaskClick?.(task)}
              data-status={task.status}
            >
              <title>{tooltipText}</title>
              <rect
                className="hg-stages__card-rect"
                width={CARD_WIDTH}
                height={CARD_HEIGHT}
                rx={6}
                ry={6}
                fill={statusFill}
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
            </g>
          );
        })}
      </svg>

      {/* Corner chip overlay: absolutely positioned HTML atop the SVG so
          each card gets a single rev-provenance chip in its top-right
          corner. Replaces the retired gen-badge + arrow badge + "(current
          Rn)" floating text — one chip, one axis. Singleton chains render
          a muted chip; multi-member chains render an interactive
          RevisionHistoryBadge (same palette, expanded history). */}
      <div
        className="hg-stages__badge-overlay"
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          width,
          height,
          pointerEvents: 'none',
        }}
        data-testid="task-stages-badge-overlay"
      >
        {Array.from(cards.values()).map(({ task, chain, x, y }) => {
          // Rev chip is only meaningful in cumulative mode (revMeta
          // carries introducedInRevision / lastModifiedInRevision). In
          // legacy "Latest only" mode we skip the chip entirely so the
          // card stays uncluttered.
          if (!revMeta) return null;
          return (
            <div
              key={`chip-${task.id}`}
              className="hg-stages__chip-slot"
              style={{
                position: 'absolute',
                // Anchor the chip's right edge to the card's right edge
                // with a small inset, sitting just above the card so it
                // doesn't overlap the status dot on the inside.
                left: x + CARD_WIDTH - 58,
                top: y - 8,
                pointerEvents: 'auto',
              }}
              data-testid={`rev-chip-for-${task.id}`}
            >
              <RevisionHistoryBadge
                chain={chain}
                currentRevision={revisionFilter}
                onClickMember={handleBadgeMemberClick}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
