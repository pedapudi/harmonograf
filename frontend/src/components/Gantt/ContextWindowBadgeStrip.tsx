import { useEffect, useState } from 'react';
import type { OverlayContext } from '../../gantt/GanttCanvas';
import { useUiStore } from '../../state/uiStore';
import { ContextWindowBadge } from './ContextWindowBadge';

// Overlay layer: anchors one ContextWindowBadge to the trailing edge of each
// visible agent row header. Lives in the GanttCanvas renderOverlay slot so it
// can read the authoritative row layout from the renderer (honoring focus,
// hide, and collapse state) without duplicating the layout math.

interface Props {
  ctx: OverlayContext;
}

export function ContextWindowBadgeStrip({ ctx }: Props) {
  const { renderer, store } = ctx;
  const contextOverlayVisible = useUiStore((s) => s.contextOverlayVisible);

  // rAF-local frame counter so the strip re-measures row layout when the
  // renderer pans/zooms (same pattern as GanttDomProxy). The parent-provided
  // ctx.tick bumps only on React-visible viewport changes; this pump covers
  // the frames that are driven imperatively inside the renderer's rAF loop.
  const [, setFrame] = useState(0);
  useEffect(() => {
    let handle = 0;
    const step = () => {
      setFrame((n) => (n + 1) | 0);
      handle = requestAnimationFrame(step);
    };
    handle = requestAnimationFrame(step);
    return () => cancelAnimationFrame(handle);
  }, []);

  if (!contextOverlayVisible) return null;

  const rows = renderer.getRowLayout().filter((r) => !r.hidden);
  // Gutter width constant — matches GUTTER_WIDTH_PX used by the renderer.
  // The badge sits flush to the right edge of the gutter column so it looks
  // like an extension of the agent row header chip.
  const GUTTER = 160;

  return (
    <div
      data-testid="context-window-badge-strip"
      style={{
        position: 'absolute',
        inset: 0,
        pointerEvents: 'none',
        zIndex: 6,
      }}
    >
      {rows.map((row) => {
        const latest = store.contextSeries.latest(row.agentId);
        if (!latest) return null;
        return (
          <div
            key={row.agentId}
            style={{
              position: 'absolute',
              left: GUTTER - 8,
              top: row.top + row.height / 2 - 10,
              transform: 'translateX(-100%)',
              pointerEvents: 'auto',
            }}
          >
            <ContextWindowBadge store={store} agentId={row.agentId} compact />
          </div>
        );
      })}
    </div>
  );
}

