// GanttZ.tsx — the hero lane-per-agent Gantt. (compose.html ganttSVG 267-312 +
// edge/arrowhead 78-81/296-299.) Lanes per agent; spans positioned by time,
// colored by KIND with STATUS treatment; now-cursor accent line; transfer +
// delegation edges as span-end→span-start S-curves with SMALL SOLID arrowheads.
// Clickable spans → inspector (onSpanSelect → useUiStore.selectSpan).
//
// KIND = hue (--hg-kind-*), STATUS = treatment (running→accent+breathe via
// .is-running, failed→--bad+✕, awaiting→wait-for-human dashed stroke+◷,
// planned→dashed). goldfive spans → --hg-gf-*. THIN line-art, token-only colors,
// fit-to-width viewBox + non-scaling strokes.
//
// SIGNATURE IS FROZEN — do not change the props.

import { type ReactNode } from 'react';
import { useUiStore } from '../../state/uiStore';
import type { ZSession, ZSpan } from './adapter';
import { KIND, statusFill } from './svgUtils';

export interface GanttZProps {
  z: ZSession;
  /** mini drops the goldfive lane/axis/labels and uses bh=6. */
  compact?: boolean;
  mini?: boolean;
  selectedSpanId?: string | null;
  /** default → useUiStore.getState().selectSpan */
  onSpanSelect?: (spanId: string) => void;
}

const W = 940;
const PAD_R = 14;

export function GanttZ({
  z,
  compact = false,
  mini = false,
  selectedSpanId = null,
  onSpanSelect,
}: GanttZProps) {
  // Default selection handler: open the existing inspector drawer.
  const select = onSpanSelect ?? ((id: string) => useUiStore.getState().selectSpan(id));

  // ── layout ─────────────────────────────────────────────────────────────────
  const rowH = compact ? 24 : 30;
  const padL = mini ? 8 : 76;
  const bh = mini ? 6 : 12;
  const T = z.T > 0 ? z.T : 30;

  // Lanes: mini drops the goldfive lane. Lane order = adapter join-time order.
  const agents = mini ? z.agents.filter((a) => a.synthetic !== 'goldfive') : z.agents;
  const H = agents.length * rowH + (mini ? 10 : 30);
  const X = (t: number): number => padL + ((W - padL - PAD_R) * t) / T;

  // Per-agent center-y, keyed by agent id. (Study keys Y by name; here by id.)
  const Y = new Map<string, number>();
  agents.forEach((a, i) => Y.set(a.id, (mini ? 5 : 12) + i * rowH + rowH / 2));

  // Empty/loading: render the grid + axis frame so the view never collapses.
  // (The adapter never returns null; an empty session has no agents/spans.)

  // ── context curve (busiest lane, study draws it on the `coder` row) ──────────
  // The adapter's z.ctx is the busiest non-synthetic agent's utilisation; place
  // it on the first non-synthetic lane present (study: coder).
  const ctxLane = agents.find((a) => a.synthetic === null);
  let ctxPath: string | null = null;
  if (!mini && !compact && z.ctx.length > 0 && ctxLane) {
    const cy = Y.get(ctxLane.id)! + rowH / 2 - 3;
    let d = `M${X(0)},${cy}`;
    for (const [t, v] of z.ctx) {
      if (t <= T) d += ` L${X(t).toFixed(1)},${(cy - v * 9).toFixed(1)}`;
    }
    ctxPath = d;
  }

  // ── edges (transfer + delegation), anchored bar-end → receiver bar-start ─────
  // spanStart: the receiver's next span starting at/after t (study 295).
  const spanStart = (agentId: string, t: number): ZSpan | null => {
    let best: ZSpan | null = null;
    for (const sp of z.spans) {
      if (sp.agent !== agentId || !Y.has(sp.agent)) continue;
      if (sp.t0 >= t - 3 && (!best || sp.t0 < best.t0)) best = sp;
    }
    return best;
  };

  return (
    <svg
      className="fig"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`execution gantt — ${z.id || 'session'}`}
    >
      {/* vertical gridlines + Ns axis labels (non-mini) */}
      {!mini &&
        Array.from({ length: 9 }, (_, i) => {
          const gx = X((T * i) / 8);
          return (
            <g key={`grid-${i}`}>
              <line className="hg-gantt-grid" x1={gx} y1={6} x2={gx} y2={H - 22} />
              <text className="gm-axis hg-gantt-axis" x={gx - 7} y={H - 8}>
                {Math.round((T * i) / 8)}s
              </text>
            </g>
          );
        })}

      {/* lane labels + separators */}
      {agents.map((a) => {
        const y = Y.get(a.id)!;
        return (
          <g key={`lane-${a.id}`}>
            {!mini && (
              <text className="hg-gantt-lane-label" x={6} y={y + 3}>
                {a.label}
              </text>
            )}
            <line
              className="hg-gantt-lane-sep"
              x1={padL}
              y1={y + rowH / 2 - 2}
              x2={W - PAD_R}
              y2={y + rowH / 2 - 2}
              opacity={0.35}
            />
          </g>
        );
      })}

      {/* context-window utilisation curve */}
      {ctxPath && (
        <path className="hg-gantt-ctxband-line" d={ctxPath}>
          <title>context window utilisation</title>
        </path>
      )}

      {/* span bars + failed/awaiting glyphs */}
      {z.spans.map((sp) => {
        const y = Y.get(sp.agent);
        if (y == null) return null;
        const x0 = X(sp.t0);
        const w = Math.max(4, X(sp.t1) - x0);
        const fill = statusFill(sp); // failed→--bad, gf→gfVar, else KIND(kind)
        const isPlanned = sp.status === 'planned';
        const cls =
          `hg-gantt-bar is-${sp.kind}` +
          (sp.status === 'running' ? ' is-running' : '') +
          (sp.status === 'planned' ? ' is-planned' : '') +
          (sp.status === 'failed' ? ' is-failed' : '') +
          (sp.status === 'pending' ? ' is-pending' : '') +
          (sp.status === 'cancelled' ? ' is-cancelled' : '') +
          (selectedSpanId === sp.id ? ' is-selected' : '');
        // awaiting → wait-for-human magenta dashed stroke (study inline attrs).
        const awaitingProps =
          sp.status === 'awaiting'
            ? {
                stroke: KIND('wait-for-human'),
                strokeDasharray: '3 2',
                strokeWidth: 1.2,
              }
            : {};
        const onActivate = (): void => select(sp.id);
        return (
          <g key={sp.id}>
            <rect
              className={cls}
              data-span={sp.id}
              x={x0}
              y={y - bh / 2}
              width={w}
              height={bh}
              rx={3}
              // planned bars are CSS-driven (rule-soft fill); others get KIND fill.
              {...(isPlanned ? {} : { fill })}
              {...awaitingProps}
              tabIndex={mini ? -1 : 0}
              role="button"
              aria-label={`${a_label(z, sp.agent)} · ${sp.label} · ${sp.status}`}
              onClick={onActivate}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onActivate();
                }
              }}
            >
              <title>
                {a_label(z, sp.agent)} · {sp.label} · {fmt(sp.t0)}–{fmt(sp.t1)}s ·{' '}
                {sp.status}
              </title>
            </rect>
            {!mini && sp.status === 'failed' && (
              <text className="hg-gantt-glyph-fail" x={x0 + w - 11} y={y - bh / 2 - 2}>
                ✕
              </text>
            )}
            {!mini && sp.status === 'awaiting' && (
              <text className="hg-gantt-glyph-wait" x={x0 + w + 3} y={y + 3.5}>
                ◷
              </text>
            )}
          </g>
        );
      })}

      {/* transfer + delegation edges with SMALL SOLID arrowheads */}
      {!mini && (
        <Edges
          z={z}
          X={X}
          Y={Y}
          T={T}
          spanStart={spanStart}
        />
      )}

      {/* now-cursor accent line + cap + label */}
      <NowCursor X={X} now={z.now} T={T} H={H} mini={mini} />
    </svg>
  );
}

// ── edges ──────────────────────────────────────────────────────────────────

interface EdgesProps {
  z: ZSession;
  X: (t: number) => number;
  Y: Map<string, number>;
  T: number;
  spanStart: (agentId: string, t: number) => ZSpan | null;
}

/**
 * Transfer + delegation edges. Each edge is a smooth cubic-bezier S anchored on
 * the source bar edge → the receiver's next span start, with a SMALL SOLID
 * triangle arrowhead landing on the receiver. (compose.html 296-307.) The
 * arrowhead is intentionally classless so its presentation attributes hold.
 */
function Edges({ z, X, Y, T, spanStart }: EdgesProps) {
  const parts: ReactNode[] = [];

  // Transfers (z.transfers, derived in the adapter from the edge set).
  z.transfers.forEach((tr, i) => {
    const y0 = Y.get(tr.from);
    const y1 = Y.get(tr.to);
    if (y0 == null || y1 == null || tr.t > T) return;
    const dst = spanStart(tr.to, tr.t);
    parts.push(
      <Edge
        key={`tr-${i}`}
        x0={X(tr.t)}
        y0={y0}
        x1={X(dst ? dst.t0 : tr.t)}
        y1={y1}
        cls=" is-transfer"
        col="var(--hg-kind-transfer)"
        tip={`transfer ${labelOf(z, tr.from)} → ${labelOf(z, tr.to)} · ${fmt(tr.t)}s`}
      />,
    );
  });

  // A single delegation block (out + rejoin), if present.
  const d = z.delegation;
  if (d) {
    const yFrom = Y.get(d.from);
    const yTo = Y.get(d.to);
    const out = spanStart(d.to, d.t0);
    if (yFrom != null && yTo != null && out && d.t0 < T) {
      parts.push(
        <Edge
          key="dlg-out"
          x0={X(d.t0)}
          y0={yFrom}
          x1={X(out.t0)}
          y1={yTo}
          cls=""
          col="var(--ink-soft)"
          tip={`delegate → ${labelOf(z, d.to)}${d.tokens != null ? ` · ${d.tokens} tok` : ''}`}
        />,
        <Edge
          key="dlg-rejoin"
          x0={X(Math.min(out.t1, T))}
          y0={yTo}
          x1={X(Math.min(d.t1, T))}
          y1={yFrom}
          cls=""
          col="var(--ink-soft)"
          tip={`rejoin${d.verdict ? ` · ${d.verdict}` : ''}`}
        />,
      );
      parts.push(
        <text
          key="dlg-ok"
          x={X(Math.min(d.t1, T)) + 3}
          y={yFrom + 16}
          fontSize={9}
          fill="var(--good)"
        >
          ✓
        </text>,
      );
    }
  }

  return <>{parts}</>;
}

interface EdgeProps {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  cls: string;
  col: string;
  tip: string;
}

/** One S-curve edge + its small solid arrowhead. (compose.html 296-299.) */
function Edge({ x0, y0, x1, y1, cls, col, tip }: EdgeProps) {
  const vs = Math.sign(y1 - y0) || 1;
  const ya = y0 + vs * 5;
  const yb = y1 - vs * 5;
  const dx = Math.max(10, Math.abs(x1 - x0) * 0.5);
  const ad = x1 >= x0 ? 1 : -1;
  return (
    <>
      <path
        className={`hg-gantt-edge${cls}`}
        d={`M${x0.toFixed(1)},${ya.toFixed(1)} C ${(x0 + dx).toFixed(1)},${ya.toFixed(
          1,
        )} ${(x1 - dx).toFixed(1)},${yb.toFixed(1)} ${x1.toFixed(1)},${yb.toFixed(1)}`}
      >
        <title>{tip}</title>
      </path>
      <path
        d={`M${(x1 - 3 * ad).toFixed(1)},${(yb - 1.4).toFixed(1)} L${x1.toFixed(
          1,
        )},${yb.toFixed(1)} L${(x1 - 3 * ad).toFixed(1)},${(yb + 1.4).toFixed(1)} Z`}
        fill={col}
        stroke="none"
        opacity={0.85}
      />
    </>
  );
}

// ── now cursor ───────────────────────────────────────────────────────────────

interface NowCursorProps {
  X: (t: number) => number;
  now: number;
  T: number;
  H: number;
  mini: boolean;
}

function NowCursor({ X, now, T, H, mini }: NowCursorProps) {
  const nx = X(Math.min(now, T));
  return (
    <>
      <line
        className="hg-gantt-now"
        x1={nx}
        y1={4}
        x2={nx}
        y2={H - (mini ? 6 : 22)}
      />
      {!mini && (
        <>
          <circle className="hg-gantt-now-cap" cx={nx} cy={4} r={2.5} />
          <text className="hg-gantt-now-label" x={nx + 5} y={11}>
            now {fmt(now)}s
          </text>
        </>
      )}
    </>
  );
}

// ── helpers ──────────────────────────────────────────────────────────────────

/** Trim trailing `.0` so the tooltip reads like the study (`16s`, not `16.0s`). */
function fmt(t: number): string {
  return Number.isInteger(t) ? String(t) : t.toFixed(1);
}

/** Resolve a lane label from an agent id (falls back to the id itself). */
function labelOf(z: ZSession, agentId: string): string {
  return z.agents.find((a) => a.id === agentId)?.label ?? agentId;
}

/** Alias kept for the span aria-label call site (readability). */
function a_label(z: ZSession, agentId: string): string {
  return labelOf(z, agentId);
}
