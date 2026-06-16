// GanttViewZ.tsx — the hero rail view: the gantt stands alone, full size, with
// the judge heartbeat underneath. (compose.html mainHybrid gantt branch 714-717.)
//
// It owns the zoom/pan VIEWPORT state for the hero gantt: a null view means
// "fit" (the full [0, T] range, identical to the pre-zoom behaviour); a non-null
// view is the visible time window. The header carries a − / + / fit toolbar, the
// GanttZ takes wheel-zoom + drag-pan (drag the timeline to scroll), and a
// full-range MinimapZ beneath both shows where the window sits — drag a region
// on it to zoom into that section, or click to recenter.

import { useRef, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import type { ZSession } from './adapter';
import { Fig } from './Fig';
import { GanttZ } from './GanttZ';
import { MinimapZ } from './MinimapZ';
import { JudgeHeartbeatZ } from './SeismographZ';
import {
  contentDomain,
  fitView,
  isFit,
  zoomView,
  type GanttView,
} from './ganttViewport';

export interface GanttViewZProps {
  z: ZSession;
}

export function GanttViewZ({ z }: GanttViewZProps) {
  const selectedSpanId = useUiStore((s) => s.selectedSpanId);
  const selectSpan = useUiStore((s) => s.selectSpan);

  // Viewport state: null = fit (full range). The effective window `eff` is what
  // every child reads; with view===null it is the full [0, T] so the gantt
  // renders exactly as it did before zoom existed.
  const [view, setView] = useState<GanttView | null>(null);
  // The viewport is bounded by the CONTENT range (first span → last span), not
  // [0, sessionEnd] — so "fit" snaps to where the spans are and skips any
  // agent-startup lead-in. Falls back to [0, T] for an empty session.
  const dom = contentDomain(z.spans, z.T);
  const eff = view ?? fitView(dom);
  const atFit = isFit(eff, dom);
  const center = (eff.t0 + eff.t1) / 2;

  // Gantt-area height. null = natural/auto (fit ALL lanes — the original
  // behaviour). A px value caps the scroll container so a tall, many-lane
  // session can be shrunk and scrolled. The drag-pill below seeds its base
  // from the container's live clientHeight on the FIRST drag so it picks up
  // exactly where the natural layout left off.
  const [ganttH, setGanttH] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Per-gesture state captured at pointerdown: where the drag started (clientY)
  // and the height we are resizing from.
  const dragRef = useRef<{ startY: number; baseH: number } | null>(null);

  const GANTT_H_MIN = 120;
  const GANTT_H_MAX = 2000;
  const clampH = (h: number): number =>
    Math.min(GANTT_H_MAX, Math.max(GANTT_H_MIN, h));

  const onResizeDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    // Seed the base from whatever height the gantt currently occupies. On the
    // first drag ganttH is still null (auto) so we read the live clientHeight;
    // thereafter we resize from the held value.
    const baseH =
      ganttH ?? scrollRef.current?.clientHeight ?? GANTT_H_MIN;
    dragRef.current = { startY: e.clientY, baseH: clampH(baseH) };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      // jsdom / unsupported — capture is a nicety, the move handler still works.
    }
  };

  const onResizeMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const dy = e.clientY - drag.startY;
    setGanttH(clampH(drag.baseH + dy));
  };

  const onResizeUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      // ignore — see onResizeDown.
    }
  };

  return (
    <>
      <div className="zk-gantt-head">
        <h3>execution — gantt</h3>
        <div className="zk-zoom" role="group" aria-label="gantt zoom">
          <button
            type="button"
            aria-label="zoom out"
            title="zoom out"
            onClick={() => setView(zoomView(eff, 1 / 0.6, center, dom))}
          >
            −
          </button>
          <button
            type="button"
            aria-label="zoom in"
            title="zoom in"
            onClick={() => setView(zoomView(eff, 0.6, center, dom))}
          >
            +
          </button>
          <button
            type="button"
            aria-label="fit to range"
            title="fit to full range"
            aria-pressed={atFit}
            disabled={atFit}
            onClick={() => setView(null)}
          >
            fit
          </button>
        </div>
      </div>
      <div
        ref={scrollRef}
        className="zk-gantt-scroll"
        style={ganttH != null ? { height: ganttH, overflowY: 'auto' } : undefined}
      >
        <div className="gantt-click">
          <Fig>
            {(w) => (
              <GanttZ
                z={z}
                W={w}
                view={eff}
                onViewChange={setView}
                selectedSpanId={selectedSpanId}
                onSpanSelect={(id) =>
                  // Toggle: clicking the already-selected span deselects it
                  // (selectSpan(null) closes the drawer + releases the pinned
                  // hovercard); clicking a different span selects that one.
                  selectSpan(id === selectedSpanId ? null : id)
                }
              />
            )}
          </Fig>
        </div>
      </div>
      <div
        className="zk-gantt-resize"
        role="separator"
        aria-orientation="horizontal"
        aria-label="resize gantt height"
        title="drag to resize the gantt height"
        onPointerDown={onResizeDown}
        onPointerMove={onResizeMove}
        onPointerUp={onResizeUp}
        onPointerCancel={onResizeUp}
      >
        <span className="zk-gantt-resize-grip" />
      </div>
      <Fig>{(w) => <MinimapZ z={z} view={eff} onViewChange={setView} W={w} />}</Fig>
      <p className="zk-prop-note" style={{ margin: '4px 2px 0' }}>
        drag the gantt to scroll · scroll-wheel to zoom · drag a region on the
        minimap to zoom into it · click the minimap to recenter
      </p>
      <h3>judge heartbeat</h3>
      <Fig>{(w) => <JudgeHeartbeatZ z={z} W={w} />}</Fig>
    </>
  );
}
