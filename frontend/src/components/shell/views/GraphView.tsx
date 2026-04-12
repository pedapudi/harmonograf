import './views.css';
import { useEffect, useMemo, useState, useCallback } from 'react';
import { useUiStore } from '../../../state/uiStore';
import { useSessionWatch } from '../../../rpc/hooks';
import { colorForAgent } from '../../../theme/agentColors';
import type { Agent, Span } from '../../../gantt/types';
import type { SessionStore } from '../../../gantt/index';

// ─── Layout constants ─────────────────────────────────────────────────────────
const NODE_W = 180;
const NODE_H = 80;
const H_GAP = 80;   // horizontal gap between nodes in the same level
const V_GAP = 110;  // vertical gap between levels
const PAD_X = 60;
const PAD_Y = 60;

// ─── Types ────────────────────────────────────────────────────────────────────

interface AgentNode {
  agent: Agent;
  x: number;
  y: number;
  w: number;
  h: number;
  color: string;
  spanCount: number;
  invocationCount: number;
  hasRunning: boolean;
  level: number;
}

type EdgeKind = 'transfer' | 'delegation';

interface AgentEdge {
  id: string;
  fromId: string;
  toId: string;
  kind: EdgeKind;
  count: number;
  // computed from node positions
  x1: number; y1: number;
  x2: number; y2: number;
}

interface GraphLayout {
  nodes: Map<string, AgentNode>;
  edges: AgentEdge[];
  width: number;
  height: number;
}

// ─── Layout computation ───────────────────────────────────────────────────────

function computeGraph(store: SessionStore): GraphLayout {
  const agents = store.agents.list;
  const allSpans = store.spans.queryRange(-Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER);

  // Build span lookup
  const spanById = new Map<string, Span>();
  for (const s of allSpans) spanById.set(s.id, s);

  // Per-agent stats
  const spanCount = new Map<string, number>();
  const invCount = new Map<string, number>();
  const hasRunning = new Map<string, boolean>();
  for (const a of agents) { spanCount.set(a.id, 0); invCount.set(a.id, 0); hasRunning.set(a.id, false); }
  for (const s of allSpans) {
    spanCount.set(s.agentId, (spanCount.get(s.agentId) ?? 0) + 1);
    if (s.kind === 'INVOCATION') invCount.set(s.agentId, (invCount.get(s.agentId) ?? 0) + 1);
    if (s.status === 'RUNNING') hasRunning.set(s.agentId, true);
  }

  // ── Derive edges ─────────────────────────────────────────────────────────
  // edgeKey → { kind, count }
  const edgeAcc = new Map<string, { kind: EdgeKind; count: number }>();

  const addEdge = (from: string, to: string, kind: EdgeKind) => {
    if (from === to) return;
    // transfer takes precedence over delegation on the same pair
    const key = `${from}→${to}`;
    const existing = edgeAcc.get(key);
    if (existing) {
      if (kind === 'transfer') existing.kind = 'transfer';
      existing.count++;
    } else {
      edgeAcc.set(key, { kind, count: 1 });
    }
  };

  // Method 1 — explicit TRANSFER spans with INVOKED links
  for (const s of allSpans) {
    if (s.kind !== 'TRANSFER') continue;
    for (const link of s.links) {
      if (link.relation !== 'INVOKED') continue;
      if (link.targetAgentId && link.targetAgentId !== s.agentId) {
        addEdge(s.agentId, link.targetAgentId, 'transfer');
      }
    }
  }

  // Method 2 — cross-agent parent-child at the INVOCATION boundary
  // Count one delegation per invocation of the called agent, not per every child span.
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    if (!s.parentSpanId) continue;
    const parent = spanById.get(s.parentSpanId);
    if (!parent || parent.agentId === s.agentId) continue;
    addEdge(parent.agentId, s.agentId, 'delegation');
  }

  // ── Hierarchical layout ───────────────────────────────────────────────────
  // Build adjacency for BFS level assignment
  const inDegree = new Map<string, number>();
  const outAdj = new Map<string, Set<string>>();
  for (const a of agents) { inDegree.set(a.id, 0); outAdj.set(a.id, new Set()); }

  for (const key of edgeAcc.keys()) {
    const [from, to] = key.split('→');
    if (!outAdj.has(from) || !inDegree.has(to)) continue;
    outAdj.get(from)!.add(to);
    inDegree.set(to, (inDegree.get(to) ?? 0) + 1);
  }

  // BFS topological level assignment
  const level = new Map<string, number>();
  const queue: string[] = [];
  for (const a of agents) {
    if ((inDegree.get(a.id) ?? 0) === 0) { level.set(a.id, 0); queue.push(a.id); }
  }
  while (queue.length) {
    const curr = queue.shift()!;
    const currLevel = level.get(curr) ?? 0;
    for (const next of (outAdj.get(curr) ?? [])) {
      const nl = currLevel + 1;
      if ((level.get(next) ?? -1) < nl) {
        level.set(next, nl);
        queue.push(next);
      }
    }
  }
  // Agents unreachable from roots (isolated or in cycles) → level 0
  for (const a of agents) { if (!level.has(a.id)) level.set(a.id, 0); }

  // Group by level, sort deterministically within level
  const byLevel = new Map<number, string[]>();
  for (const a of agents) {
    const l = level.get(a.id)!;
    const group = byLevel.get(l) ?? [];
    group.push(a.id);
    byLevel.set(l, group);
  }
  for (const g of byLevel.values()) g.sort();

  // Compute canvas size
  const maxPerLevel = Math.max(1, ...Array.from(byLevel.values()).map((g) => g.length));
  const numLevels = Math.max(1, byLevel.size);
  const canvasW = Math.max(600, PAD_X * 2 + maxPerLevel * NODE_W + (maxPerLevel - 1) * H_GAP);
  const canvasH = Math.max(400, PAD_Y * 2 + numLevels * NODE_H + (numLevels - 1) * V_GAP);

  // Position nodes
  const nodes = new Map<string, AgentNode>();
  for (const [lvl, group] of byLevel) {
    const rowW = group.length * NODE_W + (group.length - 1) * H_GAP;
    const startX = (canvasW - rowW) / 2;
    group.forEach((id, i) => {
      const agent = agents.find((a) => a.id === id)!;
      const x = startX + i * (NODE_W + H_GAP);
      const y = PAD_Y + lvl * (NODE_H + V_GAP);
      nodes.set(id, {
        agent,
        x, y, w: NODE_W, h: NODE_H,
        color: colorForAgent(id),
        spanCount: spanCount.get(id) ?? 0,
        invocationCount: invCount.get(id) ?? 0,
        hasRunning: hasRunning.get(id) ?? false,
        level: lvl,
      });
    });
  }

  // Build edge objects with positions
  const edges: AgentEdge[] = [];
  for (const [key, info] of edgeAcc) {
    const [fromId, toId] = key.split('→');
    const src = nodes.get(fromId);
    const dst = nodes.get(toId);
    if (!src || !dst) continue;

    // Exit/entry points: prefer top/bottom for vertical flow, sides for same-level
    let x1: number, y1: number, x2: number, y2: number;
    const srcCx = src.x + src.w / 2;
    const srcCy = src.y + src.h / 2;
    const dstCx = dst.x + dst.w / 2;
    const dstCy = dst.y + dst.h / 2;

    if (Math.abs(src.level - dst.level) >= 1) {
      // vertical flow
      if (dst.level > src.level) {
        x1 = srcCx; y1 = src.y + src.h;  // exit bottom
        x2 = dstCx; y2 = dst.y;           // enter top
      } else {
        x1 = srcCx; y1 = src.y;           // exit top
        x2 = dstCx; y2 = dst.y + dst.h;  // enter bottom
      }
    } else {
      // same level — exit/enter sides
      if (srcCx <= dstCx) {
        x1 = src.x + src.w; y1 = srcCy;
        x2 = dst.x;          y2 = dstCy;
      } else {
        x1 = src.x;          y1 = srcCy;
        x2 = dst.x + dst.w;  y2 = dstCy;
      }
    }

    edges.push({ id: key, fromId, toId, kind: info.kind, count: info.count, x1, y1, x2, y2 });
  }

  return { nodes, edges, width: canvasW, height: canvasH };
}

// ─── SVG helpers ──────────────────────────────────────────────────────────────

/** Cubic bezier path that curves nicely between two points. */
function bezierPath(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.abs(x2 - x1);
  const dy = Math.abs(y2 - y1);
  // Bias the control points toward the dominant axis
  const cx = dx > dy ? (x1 + x2) / 2 : x1;
  const cy = dx > dy ? y1 : (y1 + y2) / 2;
  const cx2 = dx > dy ? (x1 + x2) / 2 : x2;
  const cy2 = dx > dy ? y2 : (y1 + y2) / 2;
  return `M ${x1} ${y1} C ${cx} ${cy}, ${cx2} ${cy2}, ${x2} ${y2}`;
}

// ─── Sub-components ───────────────────────────────────────────────────────────

const STATUS_COLOR: Record<string, string> = {
  CONNECTED: '#4caf7d',
  DISCONNECTED: '#777',
  CRASHED: '#e06070',
};

function AgentNodeSvg({ node, onClick }: { node: AgentNode; onClick: () => void }) {
  const { agent, x, y, w, h, color, spanCount, invocationCount, hasRunning } = node;
  const statusDot = STATUS_COLOR[agent.status] ?? '#777';
  const label = agent.name.length > 20 ? agent.name.slice(0, 18) + '…' : agent.name;

  return (
    <g onClick={onClick} style={{ cursor: 'pointer' }} role="button" aria-label={agent.name}>
      {/* Glow for running agents */}
      {hasRunning && (
        <rect
          x={x - 4} y={y - 4} width={w + 8} height={h + 8}
          rx={12} fill={color} opacity={0.12}
          className="hg-graph__pulse"
        />
      )}
      {/* Node body */}
      <rect
        x={x} y={y} width={w} height={h} rx={8}
        fill="var(--md-sys-color-surface-container, #1e2130)"
        stroke={color}
        strokeWidth={hasRunning ? 2.5 : 1.5}
        className={hasRunning ? 'hg-graph__pulse' : undefined}
      />
      {/* Agent name */}
      <text
        x={x + w / 2} y={y + 28}
        textAnchor="middle" dominantBaseline="central"
        fill={color} fontSize={13} fontWeight={700}
      >
        {label}
      </text>
      {/* Stats row */}
      <text
        x={x + w / 2} y={y + 52}
        textAnchor="middle" dominantBaseline="central"
        fill="var(--md-sys-color-on-surface-variant, #9da3b4)" fontSize={10}
      >
        {invocationCount} inv · {spanCount} spans
      </text>
      {/* Status dot */}
      <circle cx={x + w - 14} cy={y + 14} r={4} fill={statusDot} />
    </g>
  );
}

function EdgeSvg({ edge }: { edge: AgentEdge }) {
  const isTransfer = edge.kind === 'transfer';
  const stroke = isTransfer ? '#e8953a' : '#5b8def';
  const strokeW = isTransfer ? 2.5 : 1.5;
  const opacity = isTransfer ? 0.9 : 0.6;
  const markerId = isTransfer ? 'url(#arrow-transfer)' : 'url(#arrow-delegation)';
  const d = bezierPath(edge.x1, edge.y1, edge.x2, edge.y2);
  const midX = (edge.x1 + edge.x2) / 2;
  const midY = (edge.y1 + edge.y2) / 2 - 10;
  const kindLabel = isTransfer ? 'transfer' : 'calls';

  return (
    <g>
      <path
        d={d} fill="none"
        stroke={stroke} strokeWidth={strokeW}
        markerEnd={markerId} opacity={opacity}
        strokeDasharray={isTransfer ? undefined : '6 3'}
      />
      {/* Count + kind badge */}
      <rect
        x={midX - 26} y={midY - 8}
        width={52} height={16} rx={4}
        fill="var(--md-sys-color-surface, #10131a)" opacity={0.85}
      />
      <text
        x={midX} y={midY + 1}
        textAnchor="middle" dominantBaseline="central"
        fill={stroke} fontSize={9} fontWeight={600}
      >
        {edge.count}× {kindLabel}
      </text>
    </g>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export function GraphView() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const watch = useSessionWatch(sessionId);
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!sessionId) return;
    const u1 = watch.store.spans.subscribe(() => setTick((n) => n + 1));
    const u2 = watch.store.agents.subscribe(() => setTick((n) => n + 1));
    return () => { u1(); u2(); };
  }, [sessionId, watch.store]);

  const layout = useMemo(
    () => computeGraph(watch.store),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [watch.store, watch.store.spans.size, watch.store.agents.size],
  );

  const handleAgentClick = useCallback(
    (agentId: string) => {
      // Select the most recent invocation span for this agent so the drawer opens
      const spans = watch.store.spans
        .queryRange(-Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER)
        .filter((s) => s.agentId === agentId && s.kind === 'INVOCATION')
        .sort((a, b) => b.startMs - a.startMs);
      if (spans[0]) selectSpan(spans[0].id);
    },
    [watch.store, selectSpan],
  );

  if (!sessionId) {
    return (
      <section className="hg-panel" data-testid="graph-view">
        <header className="hg-panel__header">
          <h2 className="hg-panel__title">Graph</h2>
        </header>
        <div className="hg-panel__body">
          <div className="hg-panel__empty">
            No session selected. Open the session picker (⌘K) to pick one.
          </div>
        </div>
      </section>
    );
  }

  const agentCount = watch.store.agents.size;

  if (agentCount === 0) {
    return (
      <section className="hg-panel" data-testid="graph-view">
        <header className="hg-panel__header">
          <h2 className="hg-panel__title">Graph</h2>
          <span className="hg-panel__hint">0 agents</span>
        </header>
        <div className="hg-panel__body">
          <div className="hg-panel__empty">No agents registered for this session yet.</div>
        </div>
      </section>
    );
  }

  const transferEdges = layout.edges.filter((e) => e.kind === 'transfer').length;
  const delegEdges = layout.edges.filter((e) => e.kind === 'delegation').length;

  return (
    <section className="hg-panel" data-testid="graph-view">
      <header className="hg-panel__header">
        <h2 className="hg-panel__title">Agent Graph</h2>
        <span className="hg-panel__hint">
          {agentCount} agent{agentCount !== 1 ? 's' : ''}
          {transferEdges > 0 && ` · ${transferEdges} transfer${transferEdges !== 1 ? 's' : ''}`}
          {delegEdges > 0 && ` · ${delegEdges} delegation${delegEdges !== 1 ? 's' : ''}`}
        </span>
      </header>
      <div className="hg-panel__body" style={{ overflow: 'auto' }}>
        {/* Legend */}
        <div style={{
          display: 'flex', gap: 20, padding: '8px 16px 0',
          fontSize: 11, color: 'var(--md-sys-color-on-surface-variant, #9da3b4)',
        }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <svg width={32} height={8}>
              <line x1={0} y1={4} x2={28} y2={4} stroke="#e8953a" strokeWidth={2.5} />
            </svg>
            Transfer
          </span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <svg width={32} height={8}>
              <line x1={0} y1={4} x2={28} y2={4} stroke="#5b8def" strokeWidth={1.5} strokeDasharray="6 3" />
            </svg>
            Delegation
          </span>
          {layout.edges.length === 0 && agentCount > 0 && (
            <span style={{ marginLeft: 8, opacity: 0.6 }}>
              No agent relationships detected yet — relationships appear when agents call each other.
            </span>
          )}
        </div>

        <svg
          width={layout.width}
          height={layout.height}
          style={{ display: 'block', minWidth: layout.width }}
        >
          <defs>
            <marker id="arrow-delegation" viewBox="0 0 10 10" refX={8} refY={5}
              markerWidth={6} markerHeight={6} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#5b8def" opacity={0.8} />
            </marker>
            <marker id="arrow-transfer" viewBox="0 0 10 10" refX={8} refY={5}
              markerWidth={8} markerHeight={8} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#e8953a" />
            </marker>
          </defs>

          {/* Edges (behind nodes) */}
          {layout.edges.map((e) => (
            <EdgeSvg key={e.id} edge={e} />
          ))}

          {/* Agent nodes */}
          {Array.from(layout.nodes.values()).map((node) => (
            <AgentNodeSvg
              key={node.agent.id}
              node={node}
              onClick={() => handleAgentClick(node.agent.id)}
            />
          ))}
        </svg>
      </div>
    </section>
  );
}
