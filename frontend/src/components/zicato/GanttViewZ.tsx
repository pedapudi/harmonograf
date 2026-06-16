// GanttViewZ.tsx — the hero rail view: the gantt stands alone, full size, with
// the judge heartbeat underneath. (compose.html mainHybrid gantt branch 714-717.)
//
// It owns the zoom/pan VIEWPORT state for the hero gantt: a null view means
// "fit" (the full [0, T] range, identical to the pre-zoom behaviour); a non-null
// view is the visible time window. The header carries a − / + / fit toolbar, the
// GanttZ takes wheel-zoom + drag-pan, and a full-range MinimapZ beneath both
// shows where the window sits and seeks on click/drag.

import { useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import type { ZSession } from './adapter';
import { Fig } from './Fig';
import { GanttZ } from './GanttZ';
import { MinimapZ } from './MinimapZ';
import { JudgeHeartbeatZ } from './SeismographZ';
import { fitView, isFit, zoomView, type GanttView } from './ganttViewport';

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
  const T = z.T > 0 ? z.T : 30;
  const eff = view ?? fitView(T);
  const atFit = isFit(eff, T);
  const center = (eff.t0 + eff.t1) / 2;

  return (
    <>
      <div className="zk-gantt-head">
        <h3>execution — gantt</h3>
        <div className="zk-zoom" role="group" aria-label="gantt zoom">
          <button
            type="button"
            aria-label="zoom out"
            title="zoom out"
            onClick={() => setView(zoomView(eff, 1 / 0.6, center, T))}
          >
            −
          </button>
          <button
            type="button"
            aria-label="zoom in"
            title="zoom in"
            onClick={() => setView(zoomView(eff, 0.6, center, T))}
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
      <div className="gantt-click">
        <Fig>
          {(w) => (
            <GanttZ
              z={z}
              W={w}
              view={eff}
              onViewChange={setView}
              selectedSpanId={selectedSpanId}
              onSpanSelect={(id) => selectSpan(id)}
            />
          )}
        </Fig>
      </div>
      <Fig>{(w) => <MinimapZ z={z} view={eff} onViewChange={setView} W={w} />}</Fig>
      <h3>judge heartbeat</h3>
      <Fig>{(w) => <JudgeHeartbeatZ z={z} W={w} />}</Fig>
    </>
  );
}
