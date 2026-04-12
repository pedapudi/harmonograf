// Minimap — a compact overview of the full session timeline rendered in a
// fixed-size canvas in the bottom-right corner of the Gantt view.
//
// The minimap:
//   - Draws one thin row per agent with colored rects for every span
//   - Renders a semi-transparent viewport indicator rectangle
//   - Supports click and drag to seek the main Gantt viewport

import { useEffect, useRef, useCallback } from 'react';
import type { OverlayContext } from '../../gantt/GanttCanvas';
import { kindBaseColor } from '../../gantt/colors';
import { viewportStart } from '../../gantt/viewport';

const MM_W = 180;
const MM_H = 90;
const MM_PAD = 4;          // px padding inside minimap canvas
const MM_AGENT_H = 6;      // px height per agent row in minimap
const MM_AGENT_GAP = 1;    // px gap between agent rows

// Total height of agent rows section
function agentSectionHeight(agentCount: number): number {
  if (agentCount === 0) return 0;
  return agentCount * MM_AGENT_H + (agentCount - 1) * MM_AGENT_GAP;
}

interface MinimapProps {
  // OverlayContext from GanttCanvas.renderOverlay — contains renderer, store,
  // and `tick` which increments on every viewport change/resize.
  ctx: OverlayContext;
}

export function Minimap({ ctx }: MinimapProps) {
  const { renderer, store, tick } = ctx;
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  // Track whether a pointer drag is in progress so we can pan on mousemove.
  const draggingRef = useRef(false);

  // Compute the full session time range from all spans.
  function getSessionRange(): { totalStartMs: number; totalEndMs: number } {
    const agentIds = store.spans.agentIds();
    let totalStartMs = Number.POSITIVE_INFINITY;
    let totalEndMs = 0;

    for (const agentId of agentIds) {
      const spans = store.spans.queryAgent(agentId, 0, Number.POSITIVE_INFINITY);
      for (const span of spans) {
        if (span.startMs < totalStartMs) totalStartMs = span.startMs;
        const end = span.endMs ?? store.nowMs;
        if (end > totalEndMs) totalEndMs = end;
      }
    }

    // Fallback when there are no spans yet.
    if (!isFinite(totalStartMs)) totalStartMs = 0;
    if (totalEndMs <= totalStartMs) totalEndMs = Math.max(store.nowMs, totalStartMs + 1);

    return { totalStartMs, totalEndMs };
  }

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const c = canvas.getContext('2d');
    if (!c) return;

    const dpr = window.devicePixelRatio || 1;
    const w = MM_W;
    const h = MM_H;

    // Set physical size once if it hasn't been set already.
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = w + 'px';
      canvas.style.height = h + 'px';
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    // Clear with dark background.
    c.clearRect(0, 0, w, h);
    c.fillStyle = 'rgba(10, 12, 20, 0.88)';
    c.fillRect(0, 0, w, h);

    const agents = store.agents.list;
    if (agents.length === 0) {
      // No agents yet — just draw the background.
      return;
    }

    const { totalStartMs, totalEndMs } = getSessionRange();
    const totalDuration = totalEndMs - totalStartMs;
    if (totalDuration <= 0) return;

    const drawW = w - 2 * MM_PAD;
    const xScale = drawW / totalDuration;

    // Center agent rows vertically in the minimap.
    const sectionH = agentSectionHeight(agents.length);
    const rowsTop = MM_PAD + Math.max(0, (h - 2 * MM_PAD - sectionH) / 2);

    // Draw agent rows and spans.
    for (let i = 0; i < agents.length; i++) {
      const agent = agents[i];
      const rowY = rowsTop + i * (MM_AGENT_H + MM_AGENT_GAP);

      // Subtle row background.
      c.fillStyle = 'rgba(255,255,255,0.04)';
      c.fillRect(MM_PAD, rowY, drawW, MM_AGENT_H);

      // Draw all spans for this agent.
      const spans = store.spans.queryAgent(
        agent.id,
        totalStartMs,
        totalEndMs + 1,
      );

      for (const span of spans) {
        const x = MM_PAD + (span.startMs - totalStartMs) * xScale;
        const endMs = span.endMs ?? store.nowMs;
        const rawW = (endMs - span.startMs) * xScale;
        const spanW = Math.max(1, rawW);

        const baseColor = kindBaseColor(span.kind);
        c.globalAlpha = span.replaced ? 0.25 : 0.85;
        c.fillStyle = baseColor;
        c.fillRect(x, rowY, spanW, MM_AGENT_H);
      }
    }
    c.globalAlpha = 1;

    // Draw viewport indicator rectangle.
    const vp = renderer.getViewport();
    const vpStart = viewportStart(vp);
    const vpEnd = vp.endMs;

    const vx1 = MM_PAD + (vpStart - totalStartMs) * xScale;
    const vx2 = MM_PAD + (vpEnd - totalStartMs) * xScale;
    const vxClamped = Math.max(MM_PAD, vx1);
    const vwClamped = Math.min(MM_PAD + drawW, vx2) - vxClamped;

    if (vwClamped > 0) {
      // Fill.
      c.fillStyle = 'rgba(100,140,255,0.18)';
      c.fillRect(vxClamped, MM_PAD, vwClamped, h - 2 * MM_PAD);

      // Stroke — draw only the left and right edges for clarity.
      c.strokeStyle = 'rgba(100,140,255,0.75)';
      c.lineWidth = 1;
      c.beginPath();
      // Left edge (only if not clamped to pad boundary).
      if (vx1 >= MM_PAD) {
        c.moveTo(vxClamped + 0.5, MM_PAD);
        c.lineTo(vxClamped + 0.5, h - MM_PAD);
      }
      // Right edge.
      const rx = Math.min(MM_PAD + drawW, vx2);
      if (rx <= MM_PAD + drawW) {
        c.moveTo(rx - 0.5, MM_PAD);
        c.lineTo(rx - 0.5, h - MM_PAD);
      }
      c.stroke();
    }

    // Thin border around the whole minimap.
    c.strokeStyle = 'rgba(255,255,255,0.12)';
    c.lineWidth = 1;
    c.strokeRect(0.5, 0.5, w - 1, h - 1);
  }, [renderer, store]);

  // Redraw whenever the tick changes (viewport moves, resize, span updates).
  useEffect(() => {
    draw();
  }, [draw, tick]);

  // Subscribe to span and agent store changes so the minimap refreshes when
  // new data arrives even if the viewport hasn't changed.
  useEffect(() => {
    const unsubSpans = store.spans.subscribe(() => draw());
    const unsubAgents = store.agents.subscribe(() => draw());
    return () => {
      unsubSpans();
      unsubAgents();
    };
  }, [store, draw]);

  // Convert a pointer X position on the minimap canvas to session-relative ms,
  // then seek the main viewport to center on that time.
  const seekToPointerX = useCallback(
    (clientX: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return;

      const rect = canvas.getBoundingClientRect();
      const xCanvas = clientX - rect.left;
      const drawW = MM_W - 2 * MM_PAD;
      const xFrac = Math.max(0, Math.min(1, (xCanvas - MM_PAD) / drawW));

      const { totalStartMs, totalEndMs } = getSessionRange();
      const totalDuration = totalEndMs - totalStartMs;
      if (totalDuration <= 0) return;

      const targetMs = totalStartMs + xFrac * totalDuration;

      // Center the current viewport window around targetMs, disable live follow.
      const vp = renderer.getViewport();
      const half = vp.windowMs / 2;
      renderer.setViewport({
        ...vp,
        endMs: Math.max(vp.windowMs, targetMs + half),
        liveFollow: false,
      });
    },
    [renderer, store],
  );

  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      e.stopPropagation();
      draggingRef.current = true;
      (e.currentTarget as HTMLCanvasElement).setPointerCapture(e.pointerId);
      seekToPointerX(e.clientX);
    },
    [seekToPointerX],
  );

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (!draggingRef.current) return;
      e.stopPropagation();
      seekToPointerX(e.clientX);
    },
    [seekToPointerX],
  );

  const handlePointerUp = useCallback((e: React.PointerEvent<HTMLCanvasElement>) => {
    draggingRef.current = false;
    (e.currentTarget as HTMLCanvasElement).releasePointerCapture(e.pointerId);
  }, []);

  return (
    <canvas
      ref={canvasRef}
      style={{
        position: 'absolute',
        bottom: 24,
        right: 16,
        zIndex: 20,
        borderRadius: 6,
        cursor: 'crosshair',
        // Prevent the Gantt's wheel/click handlers from firing through.
        pointerEvents: 'auto',
      }}
      width={MM_W}
      height={MM_H}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
      // Block wheel events from bubbling to the main Gantt canvas so the user
      // can scroll within the minimap area without accidentally zooming.
      onWheel={(e) => e.stopPropagation()}
      // Block click propagation so the GanttCanvas onClick handler doesn't
      // trigger span selection when clicking the minimap.
      onClick={(e) => e.stopPropagation()}
      title="Minimap — click or drag to seek"
    />
  );
}
