import './views.css';
import { useEffect, useMemo, useCallback, useState } from 'react';
import { useUiStore } from '../../../state/uiStore';
import { useSessionWatch } from '../../../rpc/hooks';
import { colorForAgent } from '../../../theme/agentColors';
import type { Span } from '../../../gantt/types';
import type { SessionStore } from '../../../gantt/index';

// ─── Layout constants ─────────────────────────────────────────────────────────
const TIME_LABEL_W = 56;
const COL_W = 200;
const HEADER_H = 70;
const ACT_W = 16;
const MIN_PX_PER_SEC = 60;
const MAX_PLOT_H = 2400;
const MIN_PLOT_H = 400;

// ─── Types ────────────────────────────────────────────────────────────────────

type ArrowKind = 'transfer' | 'delegation' | 'return';

interface SeqArrow {
  id: string;
  kind: ArrowKind;
  fromCol: number;
  toCol: number;
  yMs: number;     // time in ms (for y-position mapping)
  label: string;
}

interface ActivationBox {
  agentIdx: number;
  startMs: number;
  endMs: number | null; // null = still running
  isRunning: boolean;
}

interface SeqLayout {
  agentIds: string[];      // ordered columns
  arrows: SeqArrow[];
  activations: ActivationBox[];
  totalMs: number;         // duration of entire session in ms
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtTime(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

// ─── Layout computation ───────────────────────────────────────────────────────

function computeSequence(store: SessionStore): SeqLayout {
  const agents = store.agents.list;
  const allSpans = store.spans.queryRange(-Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER);

  const spanById = new Map<string, Span>();
  for (const s of allSpans) spanById.set(s.id, s);

  // ── Derive edges (same logic as old GraphView for topology) ───────────────
  type RawEdgeKind = 'transfer' | 'delegation';
  interface RawEdge {
    fromId: string;
    toId: string;
    kind: RawEdgeKind;
    yMs: number;
    label: string;
    spanId: string;
  }

  const transferArrows: RawEdge[] = [];
  const coveredPairs = new Set<string>(); // "fromId→toId@approxMs" to dedup

  // Method 1 — TRANSFER spans with INVOKED links
  for (const s of allSpans) {
    if (s.kind !== 'TRANSFER') continue;
    for (const link of s.links) {
      if (link.relation !== 'INVOKED') continue;
      if (!link.targetAgentId || link.targetAgentId === s.agentId) continue;
      const key = `${s.agentId}→${link.targetAgentId}@${Math.round(s.startMs / 500)}`;
      coveredPairs.add(key);
      transferArrows.push({
        fromId: s.agentId,
        toId: link.targetAgentId,
        kind: 'transfer',
        yMs: s.startMs,
        label: s.name.length > 22 ? s.name.slice(0, 21) + '…' : s.name,
        spanId: s.id,
      });
    }
  }

  // Method 2 — cross-agent INVOCATION parents (fallback/delegation)
  const delegationArrows: RawEdge[] = [];
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    if (!s.parentSpanId) continue;
    const parent = spanById.get(s.parentSpanId);
    if (!parent || parent.agentId === s.agentId) continue;
    const key = `${parent.agentId}→${s.agentId}@${Math.round(s.startMs / 500)}`;
    if (coveredPairs.has(key)) continue; // already covered by method 1
    delegationArrows.push({
      fromId: parent.agentId,
      toId: s.agentId,
      kind: 'delegation',
      yMs: s.startMs,
      label: s.name.length > 22 ? s.name.slice(0, 21) + '…' : s.name,
      spanId: s.id,
    });
  }

  const forwardArrows: RawEdge[] = [...transferArrows, ...delegationArrows];

  // ── Topological level assignment ──────────────────────────────────────────
  const inDegree = new Map<string, number>();
  const outAdj = new Map<string, Set<string>>();
  for (const a of agents) { inDegree.set(a.id, 0); outAdj.set(a.id, new Set()); }
  for (const e of forwardArrows) {
    if (!outAdj.has(e.fromId) || !inDegree.has(e.toId)) continue;
    if (!outAdj.get(e.fromId)!.has(e.toId)) {
      outAdj.get(e.fromId)!.add(e.toId);
      inDegree.set(e.toId, (inDegree.get(e.toId) ?? 0) + 1);
    }
  }

  const level = new Map<string, number>();
  const bfsQueue: string[] = [];
  for (const a of agents) {
    if ((inDegree.get(a.id) ?? 0) === 0) { level.set(a.id, 0); bfsQueue.push(a.id); }
  }
  while (bfsQueue.length) {
    const curr = bfsQueue.shift()!;
    const currLvl = level.get(curr) ?? 0;
    for (const next of (outAdj.get(curr) ?? [])) {
      const nl = currLvl + 1;
      if ((level.get(next) ?? -1) < nl) { level.set(next, nl); bfsQueue.push(next); }
    }
  }
  for (const a of agents) { if (!level.has(a.id)) level.set(a.id, 0); }

  // First-activity time per agent (for stable ordering within the same level)
  const firstActivity = new Map<string, number>();
  for (const s of allSpans) {
    const prev = firstActivity.get(s.agentId);
    if (prev === undefined || s.startMs < prev) firstActivity.set(s.agentId, s.startMs);
  }

  const agentIds = agents
    .map((a) => a.id)
    .sort((a, b) => {
      const la = level.get(a) ?? 0;
      const lb = level.get(b) ?? 0;
      if (la !== lb) return la - lb;
      return (firstActivity.get(a) ?? 0) - (firstActivity.get(b) ?? 0);
    });

  const colIdx = new Map<string, number>();
  agentIds.forEach((id, i) => colIdx.set(id, i));

  // ── Return arrows ─────────────────────────────────────────────────────────
  // For each INVOCATION span with endMs, find who called it and emit a return.
  // We also track return arrows from delegation (cross-agent parent) invocations.
  const returnArrows: SeqArrow[] = [];

  // Build a map: calledAgentId → list of forward arrows that went TO it (sorted by yMs)
  const forwardByDest = new Map<string, RawEdge[]>();
  for (const e of forwardArrows) {
    const arr = forwardByDest.get(e.toId) ?? [];
    arr.push(e);
    forwardByDest.set(e.toId, arr);
  }

  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    if (s.endMs === null) continue;

    // Find the most recent forward arrow that went TO this agent before this invocation started
    const incoming = forwardByDest.get(s.agentId) ?? [];
    const callerArrow = incoming
      .filter((e) => e.yMs <= s.startMs)
      .sort((a, b) => b.yMs - a.yMs)[0];

    if (!callerArrow) continue;
    const fromCol = colIdx.get(s.agentId);
    const toCol = colIdx.get(callerArrow.fromId);
    if (fromCol === undefined || toCol === undefined) continue;
    if (fromCol === toCol) continue;

    returnArrows.push({
      id: `return-${s.id}`,
      kind: 'return',
      fromCol,
      toCol,
      yMs: s.endMs,
      label: '↩ return',
    });
  }

  // ── Combine all arrows ────────────────────────────────────────────────────
  const arrows: SeqArrow[] = [
    ...forwardArrows
      .map((e, i): SeqArrow | null => {
        const fc = colIdx.get(e.fromId);
        const tc = colIdx.get(e.toId);
        if (fc === undefined || tc === undefined || fc === tc) return null;
        return { id: `fwd-${i}-${e.spanId}`, kind: e.kind, fromCol: fc, toCol: tc, yMs: e.yMs, label: e.label };
      })
      .filter((a): a is SeqArrow => a !== null),
    ...returnArrows,
  ];

  // ── Activation boxes ──────────────────────────────────────────────────────
  const activations: ActivationBox[] = [];
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    const idx = colIdx.get(s.agentId);
    if (idx === undefined) continue;
    activations.push({
      agentIdx: idx,
      startMs: s.startMs,
      endMs: s.endMs,
      isRunning: s.endMs === null,
    });
  }

  // ── Total time ────────────────────────────────────────────────────────────
  let totalMs = 1000;
  for (const s of allSpans) {
    const end = s.endMs ?? store.nowMs;
    if (end > totalMs) totalMs = end;
  }

  return { agentIds, arrows, activations, totalMs };
}

// ─── Status colors ────────────────────────────────────────────────────────────

const STATUS_COLOR: Record<string, string> = {
  CONNECTED: '#4caf7d',
  DISCONNECTED: '#777',
  CRASHED: '#e06070',
};

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
    () => computeSequence(watch.store),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [watch.store, watch.store.spans.size, watch.store.agents.size],
  );

  const handleAgentClick = useCallback(
    (agentId: string) => {
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
          <h2 className="hg-panel__title">Agent Graph</h2>
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
          <h2 className="hg-panel__title">Agent Graph</h2>
          <span className="hg-panel__hint">0 agents</span>
        </header>
        <div className="hg-panel__body">
          <div className="hg-panel__empty">No agents registered for this session yet.</div>
        </div>
      </section>
    );
  }

  const nowMs = watch.store.nowMs;
  const { agentIds, arrows, activations, totalMs } = layout;

  // Scale: pixels per millisecond
  const effectiveTotalMs = Math.max(totalMs, 1000);
  const rawPxPerMs = MIN_PX_PER_SEC / 1000;
  // Ensure plot is at least MIN_PLOT_H, at most MAX_PLOT_H
  const rawPlotH = effectiveTotalMs * rawPxPerMs;
  const plotH = Math.min(MAX_PLOT_H, Math.max(MIN_PLOT_H, rawPlotH));
  const pxPerMs = plotH / effectiveTotalMs;

  const svgW = Math.max(600, TIME_LABEL_W + agentIds.length * COL_W);
  const svgH = HEADER_H + plotH + 40;

  // Column center x for each agent
  const colCx = (idx: number) => TIME_LABEL_W + idx * COL_W + COL_W / 2;
  const timeY = (ms: number) => HEADER_H + ms * pxPerMs;

  // Time label positions: at each arrow + at regular intervals
  const labelMsSet = new Set<number>();
  for (const arr of arrows) labelMsSet.add(arr.yMs);
  // Also add interval marks every ~100px
  const intervalMs = Math.ceil(100 / pxPerMs / 1000) * 1000;
  for (let t = 0; t <= effectiveTotalMs; t += intervalMs) labelMsSet.add(t);
  const labelMsList = Array.from(labelMsSet).sort((a, b) => a - b);

  // Filter out labels that are too close together (< 20px apart)
  const filteredLabels: number[] = [];
  for (const ms of labelMsList) {
    const y = timeY(ms);
    if (filteredLabels.length === 0 || y - timeY(filteredLabels[filteredLabels.length - 1]) >= 20) {
      filteredLabels.push(ms);
    }
  }

  const transferCount = arrows.filter((a) => a.kind === 'transfer').length;
  const delegCount = arrows.filter((a) => a.kind === 'delegation').length;
  const returnCount = arrows.filter((a) => a.kind === 'return').length;

  return (
    <section className="hg-panel" data-testid="graph-view">
      <header className="hg-panel__header">
        <h2 className="hg-panel__title">Agent Graph</h2>
        <span className="hg-panel__hint">
          {agentCount} agent{agentCount !== 1 ? 's' : ''}
          {transferCount > 0 && ` · ${transferCount} transfer${transferCount !== 1 ? 's' : ''}`}
          {delegCount > 0 && ` · ${delegCount} delegation${delegCount !== 1 ? 's' : ''}`}
          {returnCount > 0 && ` · ${returnCount} return${returnCount !== 1 ? 's' : ''}`}
        </span>
      </header>
      <div className="hg-panel__body" style={{ overflow: 'auto' }}>
        {/* Legend */}
        <div style={{
          display: 'flex', gap: 20, padding: '0 0 10px',
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
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <svg width={32} height={8}>
              <line x1={0} y1={4} x2={28} y2={4} stroke="#888" strokeWidth={1.2} strokeDasharray="4 4" />
            </svg>
            Return
          </span>
          {arrows.length === 0 && agentCount > 0 && (
            <span style={{ marginLeft: 8, opacity: 0.6 }}>
              No agent interactions detected yet.
            </span>
          )}
        </div>

        <svg
          width={svgW}
          height={svgH}
          style={{ display: 'block', minWidth: svgW }}
        >
          <defs>
            <marker id="arr-transfer" viewBox="0 0 10 10" refX={8} refY={5}
              markerWidth={7} markerHeight={7} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#e8953a" />
            </marker>
            <marker id="arr-delegation" viewBox="0 0 10 10" refX={8} refY={5}
              markerWidth={6} markerHeight={6} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#5b8def" opacity={0.8} />
            </marker>
            <marker id="arr-return" viewBox="0 0 10 10" refX={8} refY={5}
              markerWidth={6} markerHeight={6} orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#888" />
            </marker>
          </defs>

          {/* Time labels + horizontal tick lines */}
          {filteredLabels.map((ms) => {
            const y = timeY(ms);
            return (
              <g key={`tl-${ms}`}>
                <line
                  x1={TIME_LABEL_W} y1={y} x2={svgW} y2={y}
                  stroke="var(--md-sys-color-outline-variant, #2a2f3a)"
                  strokeWidth={0.5} opacity={0.4}
                />
                <text
                  x={TIME_LABEL_W - 8} y={y}
                  textAnchor="end" dominantBaseline="middle"
                  fill="var(--md-sys-color-on-surface-variant, #9da3b4)"
                  fontSize={10}
                >
                  {fmtTime(ms)}
                </text>
              </g>
            );
          })}

          {/* Agent header boxes + lifelines */}
          {agentIds.map((agentId, idx) => {
            const agent = watch.store.agents.get(agentId);
            if (!agent) return null;
            const cx = colCx(idx);
            const color = colorForAgent(agentId);
            const isStuck = agent.stuck === true;
            const statusDot = isStuck ? '#f59e0b' : (STATUS_COLOR[agent.status] ?? '#777');
            const hasRunning = activations.some((a) => a.agentIdx === idx && a.isRunning);
            const label = agent.name.length > 18 ? agent.name.slice(0, 17) + '…' : agent.name;
            const hBoxW = COL_W - 24;
            const hBoxX = cx - hBoxW / 2;
            const hBoxY = 4;
            const hBoxH = 52;
            const borderColor = isStuck ? '#f59e0b' : color;

            return (
              <g key={agentId}>
                {/* Lifeline */}
                <line
                  x1={cx} y1={HEADER_H} x2={cx} y2={HEADER_H + plotH}
                  stroke={color} strokeWidth={1} opacity={0.25}
                  strokeDasharray="5 5"
                />

                {/* Header box */}
                <g
                  onClick={() => handleAgentClick(agentId)}
                  style={{ cursor: 'pointer' }}
                  role="button"
                  aria-label={agent.name}
                >
                  {(hasRunning || isStuck) && (
                    <rect
                      x={hBoxX - 3} y={hBoxY - 3}
                      width={hBoxW + 6} height={hBoxH + 6}
                      rx={9}
                      fill={isStuck ? '#f59e0b' : color} opacity={0.1}
                      className="hg-graph__pulse"
                    />
                  )}
                  <rect
                    x={hBoxX} y={hBoxY}
                    width={hBoxW} height={hBoxH}
                    rx={7}
                    fill={isStuck ? '#f59e0b' : color}
                    fillOpacity={0.15}
                    stroke={borderColor}
                    strokeWidth={hasRunning || isStuck ? 2 : 1.5}
                    className={isStuck ? 'hg-graph__pulse' : hasRunning ? 'hg-graph__pulse' : undefined}
                  />
                  <text
                    x={cx} y={hBoxY + 22}
                    textAnchor="middle" dominantBaseline="central"
                    fill={isStuck ? '#f59e0b' : color} fontSize={12} fontWeight={700}
                  >
                    {label}
                  </text>
                  <text
                    x={cx} y={hBoxY + 40}
                    textAnchor="middle" dominantBaseline="central"
                    fill={isStuck ? '#f59e0b' : 'var(--md-sys-color-on-surface-variant, #9da3b4)'}
                    fontSize={10}
                  >
                    {isStuck ? '⚠ stuck' : (agent.framework !== 'UNKNOWN' ? agent.framework : '')}
                  </text>
                  {/* Status dot */}
                  <circle cx={hBoxX + hBoxW - 10} cy={hBoxY + 10} r={4} fill={statusDot} />
                </g>
              </g>
            );
          })}

          {/* Activation boxes */}
          {activations.map((act, i) => {
            const cx = colCx(act.agentIdx);
            const color = colorForAgent(agentIds[act.agentIdx] ?? '');
            const y1 = timeY(act.startMs);
            const endMs = act.endMs ?? Math.max(nowMs, act.startMs + 100);
            const y2 = timeY(endMs);
            const boxH = Math.max(4, y2 - y1);
            return (
              <rect
                key={`act-${i}`}
                x={cx - ACT_W / 2}
                y={y1}
                width={ACT_W}
                height={boxH}
                rx={3}
                fill={color}
                fillOpacity={act.isRunning ? 0.85 : 0.55}
                stroke={color}
                strokeWidth={act.isRunning ? 1.5 : 0}
                className={act.isRunning ? 'hg-graph__pulse' : undefined}
              />
            );
          })}

          {/* Arrows */}
          {arrows.map((arrow) => {
            const x1 = colCx(arrow.fromCol);
            const x2 = colCx(arrow.toCol);
            const y = timeY(arrow.yMs);
            const isLeft = x2 < x1;

            let stroke: string;
            let strokeWidth: number;
            let strokeDasharray: string | undefined;
            let markerId: string;

            if (arrow.kind === 'transfer') {
              stroke = '#e8953a';
              strokeWidth = 2.5;
              strokeDasharray = undefined;
              markerId = 'url(#arr-transfer)';
            } else if (arrow.kind === 'delegation') {
              stroke = '#5b8def';
              strokeWidth = 1.5;
              strokeDasharray = '6 3';
              markerId = 'url(#arr-delegation)';
            } else {
              stroke = '#888';
              strokeWidth = 1.2;
              strokeDasharray = '4 4';
              markerId = 'url(#arr-return)';
            }

            // Offset start/end by activation box half-width
            const startX = x1 + (isLeft ? -ACT_W / 2 : ACT_W / 2);
            const endX = x2 + (isLeft ? ACT_W / 2 : -ACT_W / 2);

            // Label position: above the arrow, centered
            const labelX = (startX + endX) / 2;
            const labelY = y - 8;
            const truncLabel = arrow.label.length > 22 ? arrow.label.slice(0, 21) + '…' : arrow.label;

            return (
              <g key={arrow.id}>
                <line
                  x1={startX} y1={y} x2={endX} y2={y}
                  stroke={stroke} strokeWidth={strokeWidth}
                  strokeDasharray={strokeDasharray}
                  markerEnd={markerId}
                />
                {/* Label background */}
                <rect
                  x={labelX - 50} y={labelY - 8}
                  width={100} height={14}
                  rx={3}
                  fill="var(--md-sys-color-surface, #10131a)"
                  opacity={0.8}
                />
                <text
                  x={labelX} y={labelY}
                  textAnchor="middle" dominantBaseline="middle"
                  fill={arrow.kind === 'return' ? '#888' : stroke}
                  fontSize={9.5} fontStyle={arrow.kind === 'return' ? 'italic' : undefined}
                >
                  {truncLabel}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </section>
  );
}
