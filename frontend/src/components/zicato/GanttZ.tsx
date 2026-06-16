// GanttZ.tsx — the hero lane-per-agent Gantt. (compose.html ganttSVG 267-312 +
// edge/arrowhead 78-81/296-299.) Lanes per agent; spans positioned by time,
// colored by KIND with STATUS treatment; now-cursor accent line; transfer +
// delegation edges as span-end→span-start S-curves with SMALL SOLID arrowheads.
// Clickable spans → inspector (onSpanSelect → useUiStore.selectSpan).
//
// KIND = hue (--hg-kind-*), STATUS = treatment (running→accent+breathe via
// .is-running, failed→--bad+✕, awaiting→wait-for-human dashed stroke+◷,
// planned→dashed). goldfive spans → --hg-gf-*. THIN line-art, token-only colors,
// viewBox width = container px (responsive, via <Fig>) so the drawing never
// upscales; non-scaling strokes.

import { useEffect, useRef, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { useUiStore } from '../../state/uiStore';
import type { ZSession, ZSpan } from './adapter';
import { KIND, statusFill, uniqueId } from './svgUtils';
import { fitView, panView, zoomView, type GanttView } from './ganttViewport';

export interface GanttZProps {
  z: ZSession;
  /** viewBox width in px (pass the measured container width from <Fig>). */
  W?: number;
  /** mini drops the goldfive lane/axis/labels and uses bh=6. */
  compact?: boolean;
  mini?: boolean;
  selectedSpanId?: string | null;
  /** default → useUiStore.getState().selectSpan */
  onSpanSelect?: (spanId: string) => void;
  /** Visible time window in seconds. Default = fitView(T) → renders as today. */
  view?: GanttView;
  /** When set, wheel zooms toward the cursor + pointer-drag pans (else static). */
  onViewChange?: (v: GanttView) => void;
}

const PAD_R = 14;
/** A pointer move under this many px from press is a click (selects), not a drag. */
const DRAG_THRESHOLD = 4;

export function GanttZ({
  z,
  W = 940,
  compact = false,
  mini = false,
  selectedSpanId = null,
  onSpanSelect,
  view,
  onViewChange,
}: GanttZProps) {
  // Default selection handler: open the existing inspector drawer.
  const select = onSpanSelect ?? ((id: string) => useUiStore.getState().selectSpan(id));

  // ── layout ─────────────────────────────────────────────────────────────────
  const rowH = compact ? 24 : 30;
  const padL = mini ? 8 : 76;
  const bh = mini ? 6 : 12;
  const T = z.T > 0 ? z.T : 30;

  // The visible window. Default = the full range, so with no `view` prop the
  // figure renders identically to today (X collapses to padL + frac*plotW * t/T).
  const eff = view ?? fitView(T);
  const vSpan = eff.t1 - eff.t0 > 0 ? eff.t1 - eff.t0 : T;
  const plotW = W - padL - PAD_R;

  // Lanes: mini drops the goldfive lane. Lane order = adapter join-time order.
  const agents = mini ? z.agents.filter((a) => a.synthetic !== 'goldfive') : z.agents;
  // Time → x over the VISIBLE window. With eff = fitView(T) this is the original.
  const X = (t: number): number => padL + (plotW * (t - eff.t0)) / vSpan;

  // ── span stacking (MD3 packLanes) ───────────────────────────────────────────
  // Greedy interval packing: each agent's concurrent spans get distinct sub-
  // lanes instead of overlapping; the agent's row then grows to fit its lanes.
  const GAP = 3;
  const subPitch = bh + GAP;
  const ROWPAD = mini ? 4 : 10;
  const laneOf = new Map<string, number>();
  const laneCount = new Map<string, number>();
  agents.forEach((a) => {
    const spans = z.spans
      .filter((s) => s.agent === a.id)
      .sort((p, q) => p.t0 - q.t0 || p.t1 - q.t1);
    const laneEnds: number[] = [];
    for (const s of spans) {
      let lane = laneEnds.findIndex((end) => end <= s.t0);
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(0);
      }
      laneEnds[lane] = s.t1;
      laneOf.set(s.id, lane);
    }
    laneCount.set(a.id, Math.max(1, laneEnds.length));
  });

  // Variable per-agent row geometry; Y keeps the row CENTER (edges/ctx/label).
  const rowTop = new Map<string, number>();
  const rowHt = new Map<string, number>();
  const Y = new Map<string, number>();
  let acc = mini ? 5 : 10;
  agents.forEach((a) => {
    const h = Math.max(rowH, laneCount.get(a.id)! * subPitch - GAP + ROWPAD);
    rowTop.set(a.id, acc);
    rowHt.set(a.id, h);
    Y.set(a.id, acc + h / 2);
    acc += h;
  });
  const H = acc + (mini ? 5 : 24);

  // y-top of a span's bar within its agent row (centers the lane block).
  const spanYTop = (sp: ZSpan): number => {
    const top = rowTop.get(sp.agent);
    if (top == null) return 0;
    const blockH = laneCount.get(sp.agent)! * subPitch - GAP;
    const padTop = (rowHt.get(sp.agent)! - blockH) / 2;
    return top + padTop + (laneOf.get(sp.id) ?? 0) * subPitch;
  };

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

  // ── interactions: wheel-zoom-toward-cursor + drag-to-pan ─────────────────────
  // Only active when onViewChange is wired. A drag past DRAG_THRESHOLD suppresses
  // the trailing span-click so selection still works on a plain click. The svg
  // ref + getBoundingClientRect map clientX → svg-x → time over the live window.
  const svgRef = useRef<SVGSVGElement>(null);
  const interactive = !mini && !!onViewChange;
  const dragRef = useRef<{
    startX: number;
    moved: boolean;
    view: GanttView;
  } | null>(null);

  // Wheel-zoom toward the cursor. Attached as a NON-PASSIVE native listener (a
  // React onWheel is passive in React 19 → preventDefault would no-op + warn), so
  // the page never scrolls while zooming. The effect re-registers when the view /
  // handler changes so the closure always reads the current window.
  useEffect(() => {
    const el = svgRef.current;
    if (!el || !interactive || !onViewChange) return;
    const handler = (e: WheelEvent): void => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0) return;
      const svgX = ((e.clientX - rect.left) / rect.width) * W;
      const focusT = eff.t0 + ((svgX - padL) / plotW) * vSpan;
      // deltaY > 0 (scroll down) → zoom OUT; deltaY < 0 → zoom IN.
      const factor = e.deltaY > 0 ? 1 / 0.85 : 0.85;
      onViewChange(zoomView(eff, factor, focusT, T));
    };
    el.addEventListener('wheel', handler, { passive: false });
    return () => el.removeEventListener('wheel', handler);
  }, [interactive, onViewChange, eff, vSpan, plotW, padL, W, T]);

  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>): void => {
    if (!onViewChange || e.button !== 0) return;
    dragRef.current = { startX: e.clientX, moved: false, view: eff };
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>): void => {
    const d = dragRef.current;
    if (!d || !onViewChange) return;
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return;
    const dxPx = e.clientX - d.startX;
    if (!d.moved && Math.abs(dxPx) < DRAG_THRESHOLD) return;
    d.moved = true;
    // Pan from the drag-start view: dt = -(dxPx in svg units) → window seconds.
    const dxSvg = (dxPx / rect.width) * W;
    const dt = -(dxSvg / plotW) * vSpan;
    onViewChange(panView(d.view, dt, T));
  };

  const onPointerUp = (e: ReactPointerEvent<SVGSVGElement>): void => {
    const d = dragRef.current;
    if (d) {
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* capture may already be released */
      }
    }
    // Keep `moved` readable through the trailing click (cleared on next press).
  };

  // A span click is swallowed when the gesture was a drag (movement past thresh).
  const selectSpanGuarded = (id: string): void => {
    if (dragRef.current?.moved) {
      dragRef.current = null;
      return;
    }
    dragRef.current = null;
    select(id);
  };

  // Unique clip id so the time-varying content can't spill into the lane gutter.
  const clipId = uniqueId(`gantt-${z.id}-${W}-${H}`);

  return (
    <svg
      ref={svgRef}
      className="fig"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`execution gantt — ${z.id || 'session'}`}
      style={interactive ? { touchAction: 'none', cursor: 'grab' } : undefined}
      onPointerDown={interactive ? onPointerDown : undefined}
      onPointerMove={interactive ? onPointerMove : undefined}
      onPointerUp={interactive ? onPointerUp : undefined}
      onPointerCancel={interactive ? onPointerUp : undefined}
    >
      {/* Clip the time-varying content to the plot rect so a zoomed window
          never draws over the left lane-label gutter. */}
      <defs>
        <clipPath id={clipId}>
          <rect x={padL} y={0} width={Math.max(0, W - PAD_R - padL)} height={H} />
        </clipPath>
      </defs>
      {/* vertical gridlines + Ns axis labels (non-mini). Lines span the visible
          window so they stay aligned under zoom; clipped to the plot rect.
          Labels sit BELOW the plot (y=H-8) and are clipped too — that gutter is
          inside the clip x-range. */}
      {!mini && (
        <g clipPath={`url(#${clipId})`}>
          {Array.from({ length: 9 }, (_, i) => {
            const t = eff.t0 + (vSpan * i) / 8;
            const gx = X(t);
            return (
              <g key={`grid-${i}`}>
                <line className="hg-gantt-grid" x1={gx} y1={6} x2={gx} y2={H - 22} />
                <text className="gm-axis hg-gantt-axis" x={gx - 7} y={H - 8}>
                  {Math.round(t)}s
                </text>
              </g>
            );
          })}
        </g>
      )}

      {/* lane labels + separators — UNclipped (the left gutter + full-width sep). */}
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
              y1={rowTop.get(a.id)! + rowHt.get(a.id)! - 1}
              x2={W - PAD_R}
              y2={rowTop.get(a.id)! + rowHt.get(a.id)! - 1}
              opacity={0.35}
            />
          </g>
        );
      })}

      {/* ── time-varying content: clipped to the plot rect so a zoomed window
          never spills into the lane-label gutter. ─────────────────────────── */}
      <g clipPath={`url(#${clipId})`}>
      {/* context-window utilisation curve */}
      {ctxPath && (
        <path className="hg-gantt-ctxband-line" d={ctxPath}>
          <title>context window utilisation</title>
        </path>
      )}

      {/* span bars + failed/awaiting glyphs */}
      {z.spans.map((sp) => {
        if (!rowTop.has(sp.agent)) return null;
        const yTop = spanYTop(sp);
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
        const onActivate = (): void => selectSpanGuarded(sp.id);
        return (
          <g key={sp.id}>
            <rect
              className={cls}
              data-span={sp.id}
              x={x0}
              y={yTop}
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
              <text className="hg-gantt-glyph-fail" x={x0 + w - 11} y={yTop - 2}>
                ✕
              </text>
            )}
            {!mini && sp.status === 'awaiting' && (
              <text className="hg-gantt-glyph-wait" x={x0 + w + 3} y={yTop + bh / 2 + 3.5}>
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

      {/* now-cursor accent line + cap + label — only when the play-head (clamped
          to the session end T) falls inside the visible window. */}
      {Math.min(z.now, T) >= eff.t0 && Math.min(z.now, T) <= eff.t1 && (
        <NowCursor X={X} now={Math.min(z.now, T)} H={H} mini={mini} />
      )}
      </g>
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
  /** the play-head time, already clamped to the session end. */
  now: number;
  H: number;
  mini: boolean;
}

function NowCursor({ X, now, H, mini }: NowCursorProps) {
  const nx = X(now);
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
