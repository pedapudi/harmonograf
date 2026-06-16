// MinimapZ.tsx — the zicato-language port of Gantt/Minimap.tsx: a compact,
// full-timeline overview of the session strip beneath the hero GanttZ. It draws
// the WHOLE session range [0, T] (never the zoomed window) so the user always
// sees where the current viewport sits within the run.
//
// Encoding (Tufte / line-art, token-only colour — matches the rest of the
// zicato chrome):
//   • one thin row per z.agent; every span a small rect coloured by statusFill
//     (the SAME KIND-hue / gf / --bad encoding the hero gantt uses).
//   • a translucent --accent VIEWPORT rectangle from mmX(view.t0)..mmX(view.t1)
//     with crisp left/right edge strokes — the "you are here" indicator.
//   • a faint baseline time axis.
//
// Interaction: click OR drag anywhere recenters the main window on the pointer
// time, KEEPING the current width, via onViewChange(panView(...)). The pointer
// time is mapped through an svg ref + getBoundingClientRect, mirroring GanttZ's
// clientX→time math but over the full [0, T] domain.
//
// It is rendered inside <Fig> by GanttViewZ, so it accepts a MEASURED W (1:1,
// no upscaling) exactly like GanttZ. Signatures are part of the frozen contract.

import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import type { ZSession } from './adapter';
import { statusFill } from './svgUtils';
import {
  clampWindow,
  contentDomain,
  panView,
  type GanttView,
} from './ganttViewport';

export interface MinimapZProps {
  z: ZSession;
  /** the CURRENT visible window (= GanttViewZ's eff). The viewport rect tracks it. */
  view: GanttView;
  /** recenter the main window on a pointer time (keeps width) via panView. */
  onViewChange: (v: GanttView) => void;
  /** viewBox width in px (measured container width from <Fig>). */
  W?: number;
}

/** A drag past this many px is a brush-zoom; below it, a click-to-pan. */
const BRUSH_THRESHOLD_PX = 4;

// Plot frame — narrow gutter (the minimap carries no lane labels), matched to
// the hero PAD_R so the right edges line up under the gantt above.
const PAD_L = 8;
const PAD_R = 14;
const ROW_H = 6; // px per agent row
const ROW_GAP = 2; // px between agent rows
const TOP = 5; // px above the first row
const AXIS_H = 12; // px reserved for the baseline time axis

export function MinimapZ({ z, view, onViewChange, W = 940 }: MinimapZProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  // Drag gesture state: the press anchor (clientX + its time) and whether the
  // pointer has moved past the brush threshold yet. A live brush selection
  // (in seconds) is mirrored into React state so the selection rectangle draws.
  const dragRef = useRef<{ startClientX: number; startT: number; moved: boolean } | null>(
    null,
  );
  const [brush, setBrush] = useState<{ t0: number; t1: number } | null>(null);

  const T = z.T > 0 ? z.T : 30;
  const agents = z.agents;
  const plotW = W - PAD_L - PAD_R;

  // The minimap spans the CONTENT range ([first span, last span]) — the same
  // domain the gantt is bounded to — so the overview matches "fit" and carries
  // no empty agent-startup lead-in.
  const dom = contentDomain(z.spans, T);
  const domSpan = dom.hi - dom.lo > 0 ? dom.hi - dom.lo : 1;

  // Content-range time → x. The minimap ALWAYS spans the full content range
  // (it is the overview), independent of the zoomed `view`.
  const mmX = (t: number): number => PAD_L + (plotW * (t - dom.lo)) / domSpan;

  // Row geometry. Keep a sane minimum height so an empty/agent-less session
  // still draws a frame + axis instead of collapsing.
  const rowsH = agents.length > 0 ? agents.length * (ROW_H + ROW_GAP) - ROW_GAP : ROW_H;
  const H = TOP + rowsH + AXIS_H;
  const rowTop = (i: number): number => TOP + i * (ROW_H + ROW_GAP);
  const agentRow = new Map<string, number>();
  agents.forEach((a, i) => agentRow.set(a.id, i));

  // ── pointer → time over the content-range domain (mirrors GanttZ's mapping) ──
  const clientXToT = (clientX: number): number | null => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return null;
    const svgX = ((clientX - rect.left) / rect.width) * W;
    return dom.lo + (plotW > 0 ? (svgX - PAD_L) / plotW : 0) * domSpan;
  };

  // Click (no drag) → recenter the current window on the pointer time, keeping
  // its width (a quick scrub, like the MD3 minimap).
  const panToT = (targetT: number): void => {
    const center = (view.t0 + view.t1) / 2;
    onViewChange(panView(view, targetT - center, dom));
  };

  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>): void => {
    if (e.button !== 0) return;
    const t = clientXToT(e.clientX);
    if (t === null) return;
    dragRef.current = { startClientX: e.clientX, startT: t, moved: false };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* capture may be unavailable (jsdom) */
    }
  };

  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>): void => {
    const d = dragRef.current;
    if (!d) return;
    if (!d.moved && Math.abs(e.clientX - d.startClientX) < BRUSH_THRESHOLD_PX) return;
    d.moved = true;
    const t = clientXToT(e.clientX);
    if (t === null) return;
    // Live brush selection: drag a region to define the zoom window.
    setBrush({ t0: d.startT, t1: t });
  };

  const onPointerUp = (e: ReactPointerEvent<SVGSVGElement>): void => {
    const d = dragRef.current;
    dragRef.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* capture may already be released */
    }
    if (!d) return;
    if (d.moved) {
      // Brushed a region → zoom the gantt to it.
      const end = clientXToT(e.clientX) ?? d.startT;
      onViewChange(clampWindow(d.startT, end, dom));
    } else {
      // A plain click → recenter (pan) on the press point.
      panToT(d.startT);
    }
    setBrush(null);
  };

  // ── viewport indicator geometry (clamped into the plot rect) ─────────────────
  const vx0 = Math.max(PAD_L, mmX(Math.max(dom.lo, Math.min(dom.hi, view.t0))));
  const vx1 = Math.min(W - PAD_R, mmX(Math.max(dom.lo, Math.min(dom.hi, view.t1))));
  const vw = Math.max(1, vx1 - vx0);
  const axisY = H - AXIS_H + 4;

  // ── live brush selection geometry (while dragging a zoom region) ─────────────
  const brushX0 = brush
    ? Math.max(PAD_L, mmX(Math.max(dom.lo, Math.min(dom.hi, Math.min(brush.t0, brush.t1)))))
    : 0;
  const brushX1 = brush
    ? Math.min(W - PAD_R, mmX(Math.max(dom.lo, Math.min(dom.hi, Math.max(brush.t0, brush.t1)))))
    : 0;

  return (
    <svg
      ref={svgRef}
      className="fig zk-minimap"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label={`gantt minimap — ${z.id || 'session'}`}
      style={{ touchAction: 'none', cursor: 'crosshair' }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      {/* per-agent rows: a faint row track + every span as a small rect coloured
          by statusFill (the same KIND-hue / gf / --bad encoding as the hero). */}
      {agents.map((a, i) => {
        const y = rowTop(i);
        return (
          <g key={`mm-row-${a.id}`}>
            <rect
              className="zk-mm-track"
              x={PAD_L}
              y={y}
              width={Math.max(0, plotW)}
              height={ROW_H}
              fill="var(--rule-soft)"
              opacity={0.5}
            />
            {z.spans.map((sp) => {
              if (sp.agent !== a.id) return null;
              const t0 = Math.max(dom.lo, Math.min(dom.hi, sp.t0));
              const t1 = Math.max(t0, Math.min(dom.hi, sp.t1));
              const x = mmX(t0);
              const w = Math.max(1, mmX(t1) - x);
              return (
                <rect
                  key={`mm-${sp.id}`}
                  className="zk-mm-span"
                  x={x}
                  y={y}
                  width={w}
                  height={ROW_H}
                  fill={statusFill(sp)}
                  opacity={sp.status === 'cancelled' ? 0.35 : 0.85}
                />
              );
            })}
          </g>
        );
      })}

      {/* faint baseline time axis across the full range. */}
      <line
        className="hg-gantt-grid"
        x1={PAD_L}
        y1={axisY}
        x2={W - PAD_R}
        y2={axisY}
        opacity={0.7}
      />

      {/* the --accent viewport rectangle: translucent fill + crisp edge strokes.
          Drawn last so it sits above the span rects. */}
      <rect
        className="zk-mm-viewport"
        x={vx0}
        y={TOP - 2}
        width={vw}
        height={rowsH + 4}
        fill="var(--accent)"
        fillOpacity={0.14}
        stroke="none"
      >
        <title>{`viewport · ${fmt(view.t0)}–${fmt(view.t1)}s of ${fmt(T)}s`}</title>
      </rect>
      <line
        className="zk-mm-viewport-edge"
        x1={vx0}
        y1={TOP - 2}
        x2={vx0}
        y2={TOP + rowsH + 2}
        stroke="var(--accent)"
        strokeWidth={1}
        vectorEffect="non-scaling-stroke"
      />
      <line
        className="zk-mm-viewport-edge"
        x1={vx1}
        y1={TOP - 2}
        x2={vx1}
        y2={TOP + rowsH + 2}
        stroke="var(--accent)"
        strokeWidth={1}
        vectorEffect="non-scaling-stroke"
      />

      {/* live brush selection: the region being dragged to zoom into. Drawn on
          top of the viewport rect with a brighter outline so the target window
          is unmistakable while dragging. */}
      {brush && brushX1 - brushX0 >= 1 && (
        <rect
          className="zk-mm-brush"
          data-testid="zk-mm-brush"
          x={brushX0}
          y={TOP - 2}
          width={brushX1 - brushX0}
          height={rowsH + 4}
          fill="var(--accent)"
          fillOpacity={0.22}
          stroke="var(--accent)"
          strokeWidth={1}
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  );
}

/** Trim trailing `.0` so the tooltip reads like the rest of zicato (`16s`). */
function fmt(t: number): string {
  return Number.isInteger(t) ? String(t) : t.toFixed(1);
}
