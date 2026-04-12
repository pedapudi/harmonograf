import './views.css';
import { useEffect, useMemo, useState, useCallback } from 'react';
import { useUiStore } from '../../../state/uiStore';
import { useSessionWatch } from '../../../rpc/hooks';
import { usePopoverStore } from '../../../state/popoverStore';
import { kindBaseColor } from '../../../gantt/colors';
import { colorForAgent } from '../../../theme/agentColors';
import type { Span, SpanKind } from '../../../gantt/types';
import type { SessionStore } from '../../../gantt/index';

// --- Layout constants ---
const COL_WIDTH = 200;
const COL_PAD = 20;
const HEADER_H = 50;
const ROW_GAP = 46;
const NODE_W = 140;
const NODE_H_INVOCATION = 36;
const NODE_H_CHILD = 28;
const TRANSFER_STROKE = 3;
const PARENT_STROKE = 1.2;

// --- Border colors by kind ---
const KIND_BORDER: Record<SpanKind, string> = {
  INVOCATION: '#5b8def',
  LLM_CALL: '#a37ede',
  TOOL_CALL: '#4caf7d',
  TRANSFER: '#e8953a',
  USER_MESSAGE: '#6eb5d6',
  AGENT_MESSAGE: '#6eb5d6',
  WAIT_FOR_HUMAN: '#e06070',
  PLANNED: '#888',
  CUSTOM: '#888',
};

// --- Types ---
interface NodeLayout {
  id: string;
  span: Span;
  x: number;
  y: number;
  w: number;
  h: number;
  fill: string;
  border: string;
}

interface EdgeLayout {
  id: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  thick: boolean;
  color: string;
  label: string | null;
  curved: boolean;
}

interface AgentCol {
  id: string;
  name: string;
  x: number;
  color: string;
}

interface Layout {
  nodes: NodeLayout[];
  edges: EdgeLayout[];
  agentCols: AgentCol[];
  width: number;
  height: number;
}

function computeLayout(store: SessionStore): Layout {
  const agents = store.agents.list;
  const agentCols: AgentCol[] = agents.map((a, i) => ({
    id: a.id,
    name: a.name,
    x: COL_PAD + i * COL_WIDTH + COL_WIDTH / 2,
    color: colorForAgent(a.id),
  }));
  const agentXMap = new Map(agentCols.map((c) => [c.id, c.x]));

  const allSpans = store.spans
    .queryRange(-Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER)
    .slice()
    .sort((a, b) => a.startMs - b.startMs);

  // Assign y positions per agent column to avoid overlap
  const agentNextY = new Map<string, number>();
  for (const a of agents) agentNextY.set(a.id, HEADER_H + 20);

  const nodeMap = new Map<string, NodeLayout>();
  const nodes: NodeLayout[] = [];
  const edges: EdgeLayout[] = [];

  for (const span of allSpans) {
    const ax = agentXMap.get(span.agentId);
    if (ax === undefined) continue;

    const isInvocation = span.kind === 'INVOCATION';
    const h = isInvocation ? NODE_H_INVOCATION : NODE_H_CHILD;
    const w = isInvocation ? NODE_W : NODE_W - 20;

    const curY = agentNextY.get(span.agentId) ?? HEADER_H + 20;
    const y = curY;
    agentNextY.set(span.agentId, y + h + ROW_GAP - 10);

    const fill = kindBaseColor(span.kind);
    const border = KIND_BORDER[span.kind] ?? '#888';

    const node: NodeLayout = {
      id: span.id,
      span,
      x: ax - w / 2,
      y,
      w,
      h,
      fill,
      border,
    };
    nodes.push(node);
    nodeMap.set(span.id, node);
  }

  // Build edges
  for (const span of allSpans) {
    const child = nodeMap.get(span.id);
    if (!child) continue;

    // Parent → child edge
    if (span.parentSpanId) {
      const parent = nodeMap.get(span.parentSpanId);
      if (parent) {
        edges.push({
          id: `p-${parent.id}-${child.id}`,
          x1: parent.x + parent.w / 2,
          y1: parent.y + parent.h,
          x2: child.x + child.w / 2,
          y2: child.y,
          thick: false,
          color: '#555',
          label: null,
          curved: false,
        });
      }
    }

    // TRANSFER / INVOKED links — inter-agent arrows
    if (span.kind === 'TRANSFER' && span.links.length > 0) {
      for (const link of span.links) {
        if (link.relation !== 'INVOKED') continue;
        const targetNode = nodeMap.get(link.targetSpanId);
        const sourceX = agentXMap.get(span.agentId);
        const targetX = agentXMap.get(link.targetAgentId);
        if (sourceX === undefined || targetX === undefined) continue;

        const srcNode = nodeMap.get(span.id);
        if (!srcNode) continue;

        const targetAgent = store.agents.get(link.targetAgentId);
        const label = targetAgent ? `\u2192 ${targetAgent.name}` : null;

        edges.push({
          id: `t-${span.id}-${link.targetSpanId}`,
          x1: srcNode.x + srcNode.w / 2,
          y1: srcNode.y + srcNode.h / 2,
          x2: targetNode
            ? targetNode.x + targetNode.w / 2
            : targetX,
          y2: targetNode ? targetNode.y : srcNode.y,
          thick: true,
          color: '#e8a33a',
          label,
          curved: true,
        });
      }
    }
  }

  const maxY = nodes.reduce((m, n) => Math.max(m, n.y + n.h), 0);
  const width = Math.max(600, agents.length * COL_WIDTH + COL_PAD * 2);
  const height = Math.max(400, maxY + 60);

  return { nodes, edges, agentCols, width, height };
}

// --- Sub-components ---

function AgentColumnHeader({ col }: { col: AgentCol }) {
  return (
    <g>
      {/* Vertical swimlane line */}
      <line
        x1={col.x}
        y1={HEADER_H - 4}
        x2={col.x}
        y2={9999}
        stroke={col.color}
        strokeWidth={0.5}
        opacity={0.18}
      />
      {/* Agent name */}
      <text
        x={col.x}
        y={HEADER_H - 16}
        textAnchor="middle"
        fill={col.color}
        fontSize={13}
        fontWeight={600}
      >
        {col.name}
      </text>
    </g>
  );
}

function EdgePath({ edge }: { edge: EdgeLayout }) {
  let d: string;
  if (edge.curved) {
    const my = (edge.y1 + edge.y2) / 2;
    const dx = Math.abs(edge.x2 - edge.x1) * 0.35;
    d = `M ${edge.x1} ${edge.y1} C ${edge.x1 + (edge.x2 > edge.x1 ? dx : -dx)} ${my}, ${edge.x2 - (edge.x2 > edge.x1 ? dx : -dx)} ${my}, ${edge.x2} ${edge.y2}`;
  } else {
    d = `M ${edge.x1} ${edge.y1} L ${edge.x2} ${edge.y2}`;
  }

  return (
    <g>
      <path
        d={d}
        fill="none"
        stroke={edge.color}
        strokeWidth={edge.thick ? TRANSFER_STROKE : PARENT_STROKE}
        markerEnd={edge.thick ? 'url(#arrow-transfer)' : 'url(#arrow-parent)'}
        opacity={edge.thick ? 0.9 : 0.45}
      />
      {edge.label && (
        <text
          x={(edge.x1 + edge.x2) / 2}
          y={(edge.y1 + edge.y2) / 2 - 8}
          textAnchor="middle"
          fill="#e8a33a"
          fontSize={10}
          fontWeight={600}
        >
          {edge.label}
        </text>
      )}
    </g>
  );
}

function SpanNode({
  node,
  onClick,
}: {
  node: NodeLayout;
  onClick: (e: React.MouseEvent) => void;
}) {
  const isRunning = node.span.status === 'RUNNING';
  const truncName =
    node.span.name.length > 18
      ? node.span.name.slice(0, 16) + '\u2026'
      : node.span.name;

  return (
    <g onClick={onClick} style={{ cursor: 'pointer' }}>
      <rect
        x={node.x}
        y={node.y}
        width={node.w}
        height={node.h}
        rx={6}
        fill={node.fill}
        fillOpacity={0.25}
        stroke={node.border}
        strokeWidth={isRunning ? 2 : 1.2}
        className={isRunning ? 'hg-graph__pulse' : undefined}
      />
      <text
        x={node.x + node.w / 2}
        y={node.y + node.h / 2 + 1}
        textAnchor="middle"
        dominantBaseline="central"
        fill="#e3e4ea"
        fontSize={11}
        fontWeight={node.span.kind === 'INVOCATION' ? 600 : 400}
      >
        {truncName}
      </text>
      {/* Kind badge */}
      <text
        x={node.x + node.w - 4}
        y={node.y + 10}
        textAnchor="end"
        fill={node.border}
        fontSize={8}
        opacity={0.7}
      >
        {node.span.kind === 'INVOCATION'
          ? 'INV'
          : node.span.kind === 'LLM_CALL'
            ? 'LLM'
            : node.span.kind === 'TOOL_CALL'
              ? 'TOOL'
              : node.span.kind === 'TRANSFER'
                ? 'XFER'
                : node.span.kind.slice(0, 4)}
      </text>
    </g>
  );
}

// --- Main component ---

export function GraphView() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const watch = useSessionWatch(sessionId);
  const openPopover = usePopoverStore((s) => s.openForSpan);
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!sessionId) return;
    const unsub1 = watch.store.spans.subscribe(() => setTick((n) => n + 1));
    const unsub2 = watch.store.agents.subscribe(() => setTick((n) => n + 1));
    return () => {
      unsub1();
      unsub2();
    };
  }, [sessionId, watch.store]);

  const layout = useMemo(
    () => computeLayout(watch.store),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [watch.store, watch.store.spans.size, watch.store.agents.size],
  );

  const handleNodeClick = useCallback(
    (span: Span, e: React.MouseEvent) => {
      openPopover(span.id, e.clientX, e.clientY);
    },
    [openPopover],
  );

  if (!sessionId) {
    return (
      <section className="hg-panel" data-testid="graph-view">
        <header className="hg-panel__header">
          <h2 className="hg-panel__title">Graph</h2>
        </header>
        <div className="hg-panel__body">
          <div className="hg-panel__empty">
            No session selected. Open the session picker (\u2318K) to pick one.
          </div>
        </div>
      </section>
    );
  }

  if (layout.nodes.length === 0) {
    return (
      <section className="hg-panel" data-testid="graph-view">
        <header className="hg-panel__header">
          <h2 className="hg-panel__title">Graph</h2>
          <span className="hg-panel__hint">0 spans</span>
        </header>
        <div className="hg-panel__body">
          <div className="hg-panel__empty">
            No spans yet for this session.
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="hg-panel" data-testid="graph-view">
      <header className="hg-panel__header">
        <h2 className="hg-panel__title">Graph</h2>
        <span className="hg-panel__hint">
          {layout.nodes.length} span(s) &middot; {layout.agentCols.length} agent(s)
        </span>
      </header>
      <div className="hg-panel__body" style={{ overflow: 'auto' }}>
        <svg
          width={layout.width}
          height={layout.height}
          style={{ display: 'block', minWidth: layout.width }}
        >
          <defs>
            <marker
              id="arrow-parent"
              viewBox="0 0 10 10"
              refX={9}
              refY={5}
              markerWidth={6}
              markerHeight={6}
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#555" />
            </marker>
            <marker
              id="arrow-transfer"
              viewBox="0 0 10 10"
              refX={9}
              refY={5}
              markerWidth={8}
              markerHeight={8}
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#e8a33a" />
            </marker>
          </defs>

          {/* Agent column headers + swimlane lines */}
          {layout.agentCols.map((col) => (
            <AgentColumnHeader key={col.id} col={col} />
          ))}

          {/* Edges (behind nodes) */}
          {layout.edges.map((e) => (
            <EdgePath key={e.id} edge={e} />
          ))}

          {/* Nodes */}
          {layout.nodes.map((n) => (
            <SpanNode
              key={n.id}
              node={n}
              onClick={(e) => handleNodeClick(n.span, e)}
            />
          ))}
        </svg>
      </div>
    </section>
  );
}
