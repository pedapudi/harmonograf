import { useMemo } from 'react';
import type { Task, TaskPlan } from '../../gantt/types';
import { computeStages } from '../../gantt/stages';
import './TaskStagesGraph.css';

interface TaskStagesGraphProps {
  plan: TaskPlan;
  onTaskClick?: (task: Task) => void;
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

const COLUMN_WIDTH = 140;
const CARD_WIDTH = 120;
const CARD_HEIGHT = 38;
const CARD_GAP = 10;
const TOP_PADDING = 28; // for stage labels + progress badges
const COL_PADDING = 10;
const SIDE_PADDING = 12;
const AGENT_STRIPE_W = 4;

// Intrinsic DAG SVG width for a given plan. Exposed so siblings (e.g.
// InterventionsTimeline in GanttView) can visually align to the DAG
// without introducing DOM refs / ResizeObservers. Returns 0 for empty
// plans so callers can fall back to their own defaults.
export function computePlanDagWidth(plan: TaskPlan): number {
  const stages = computeStages(plan);
  if (stages.length === 0) return 0;
  return SIDE_PADDING * 2 + stages.length * COLUMN_WIDTH;
}

interface LaidOutCard {
  task: Task;
  x: number;
  y: number;
  stage: number;
}

export function TaskStagesGraph({
  plan,
  onTaskClick,
  selectedTaskId = null,
  agentColorFor,
  agentNameFor,
}: TaskStagesGraphProps) {
  const layout = useMemo(() => {
    const stages = computeStages(plan);
    if (stages.length === 0) return null;

    const cards = new Map<string, LaidOutCard>();
    let maxRows = 0;
    stages.forEach((stageTasks, stageIdx) => {
      if (stageTasks.length > maxRows) maxRows = stageTasks.length;
      stageTasks.forEach((task, rowIdx) => {
        const x = SIDE_PADDING + stageIdx * COLUMN_WIDTH + COL_PADDING;
        const y = TOP_PADDING + rowIdx * (CARD_HEIGHT + CARD_GAP);
        cards.set(task.id, { task, x, y, stage: stageIdx });
      });
    });

    const width = SIDE_PADDING * 2 + stages.length * COLUMN_WIDTH;
    const contentHeight =
      TOP_PADDING + maxRows * (CARD_HEIGHT + CARD_GAP) + 8;
    const height = Math.max(contentHeight, 116);

    // Edge routing: only keep edges whose endpoints land in different stages
    // (forward edges). Any cycle-stranded tasks all sit in stage 0 and their
    // edges to each other would be horizontal — skip them to avoid confusion.
    const edges: Array<{ from: LaidOutCard; to: LaidOutCard }> = [];
    for (const e of plan.edges) {
      const from = cards.get(e.fromTaskId);
      const to = cards.get(e.toTaskId);
      if (!from || !to) continue;
      if (to.stage <= from.stage) continue;
      edges.push({ from, to });
    }

    return { stages, cards, width, height, edges };
  }, [plan]);

  if (!layout) return null;
  const { stages, cards, width, height, edges } = layout;

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

        {stages.map((stageTasks, stageIdx) => {
          const x = SIDE_PADDING + stageIdx * COLUMN_WIDTH + COLUMN_WIDTH / 2;
          const total = stageTasks.length;
          const done = stageTasks.filter((t) => t.status === 'COMPLETED').length;
          // 0 complete → grey; some complete → blue; all complete → green.
          const badgeClass =
            done === 0
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

        {edges.map(({ from, to }, i) => {
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
          const classes = [
            'hg-stages__card',
            selected ? 'hg-stages__card--selected' : '',
            running ? 'hg-stages__card--running' : '',
          ]
            .filter(Boolean)
            .join(' ');
          // harmonograf#110 / goldfive#205: tooltip includes the cancel
          // reason on CANCELLED / FAILED cards so hovering tells the
          // operator why a task ended the way it did without a click.
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
            </g>
          );
        })}
      </svg>
    </div>
  );
}
