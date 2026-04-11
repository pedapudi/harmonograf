import { useEffect, useState } from 'react';
import type { OverlayContext } from '../../gantt/GanttCanvas';
import {
  GUTTER_WIDTH_PX,
  ROW_HEIGHT_FOCUSED_PX,
  ROW_HEIGHT_PX,
  SUB_LANE_HEIGHT_PX,
  TOP_MARGIN_PX,
  msToPx,
  viewportStart,
} from '../../gantt/viewport';
import type { Span } from '../../gantt/types';

// Invisible DOM overlay mirroring the canvas Gantt layout for Playwright
// selector stability. Task #14.
//
// The canvas renderer draws pixels, not DOM nodes, so test runners can't
// query spans directly. This component projects the renderer's per-viewport
// layout into a small set of absolutely-positioned, pointer-events:none divs
// tagged with stable `data-testid` + `data-*` attributes. Clicks still land
// on the canvas below (pointerEvents: 'none' on the proxy layer).
//
// We emit proxies only for spans currently in the viewport — matching the
// canvas culling budget — so even a 10k-span session produces O(visible)
// nodes rather than O(total). A rAF ticker repositions proxies each frame
// the canvas could have moved; if the user isn't interacting the tick is
// cheap because the layout is memoized against viewport identity.

interface Props {
  ctx: OverlayContext;
}

interface RowLayout {
  agentId: string;
  top: number;
  height: number;
}

interface SpanProxy {
  id: string;
  agentId: string;
  kind: string;
  status: string;
  name: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export function GanttDomProxy({ ctx }: Props) {
  const { renderer, store, widthCss, heightCss, tick } = ctx;
  // Internal rAF-driven tick so proxies follow pan/zoom/live-follow even when
  // the parent renderOverlay callback isn't re-invoked by React (the renderer
  // drives pan/zoom imperatively via its own frame loop).
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    let handle = 0;
    const loop = () => {
      setFrame((n) => (n + 1) | 0);
      handle = requestAnimationFrame(loop);
    };
    handle = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(handle);
  }, []);
  void tick;
  void frame;

  const viewport = renderer.getViewport();
  const vs = viewportStart(viewport);
  const ve = viewport.endMs;

  const rows: RowLayout[] = [];
  {
    let y = TOP_MARGIN_PX;
    const focusedId =
      (renderer as unknown as { focusedAgentId: string | null }).focusedAgentId ??
      null;
    for (const agent of store.agents.list) {
      const h = agent.id === focusedId ? ROW_HEIGHT_FOCUSED_PX : ROW_HEIGHT_PX;
      rows.push({ agentId: agent.id, top: y, height: h });
      y += h;
    }
  }

  const proxies: SpanProxy[] = [];
  {
    const scratch: Span[] = [];
    for (const row of rows) {
      scratch.length = 0;
      store.spans.queryAgent(row.agentId, vs, ve, scratch);
      for (const s of scratch) {
        const x1 = Math.max(
          GUTTER_WIDTH_PX + 10,
          msToPx(viewport, widthCss, s.startMs),
        );
        const x2 = msToPx(viewport, widthCss, s.endMs ?? store.nowMs);
        const width = Math.max(2, x2 - x1);
        const laneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(row.height / 3));
        const laneTop = row.top + 2 + (s.lane >= 0 ? s.lane : 0) * laneH;
        const rectH = Math.max(
          6,
          Math.min(row.top + row.height - 2, laneTop + laneH - 2) - laneTop,
        );
        proxies.push({
          id: s.id,
          agentId: row.agentId,
          kind: s.kind,
          status: s.status,
          name: s.name,
          x: x1,
          y: laneTop,
          w: width,
          h: rectH,
        });
      }
    }
  }

  return (
    <div
      aria-hidden="true"
      style={{
        position: 'absolute',
        inset: 0,
        width: widthCss,
        height: heightCss,
        pointerEvents: 'none',
        zIndex: 3,
      }}
    >
      {rows.map((row) => (
        <div
          key={row.agentId}
          data-testid="gantt-agent-row"
          data-agent-id={row.agentId}
          style={{
            position: 'absolute',
            left: GUTTER_WIDTH_PX,
            top: row.top,
            width: widthCss - GUTTER_WIDTH_PX,
            height: row.height,
          }}
        />
      ))}
      {proxies.map((p) => (
        <div
          key={p.id}
          data-testid="gantt-span-block"
          data-span-id={p.id}
          data-agent-id={p.agentId}
          data-span-kind={p.kind}
          data-span-status={p.status}
          data-span-name={p.name}
          style={{
            position: 'absolute',
            left: p.x,
            top: p.y,
            width: p.w,
            height: p.h,
          }}
        />
      ))}
    </div>
  );
}
