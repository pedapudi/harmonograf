import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { GanttRenderer, type HoverState } from './renderer';
import type { SessionStore } from './index';
import { useThemeStore } from '../theme/store';
import { useUiStore } from '../state/uiStore';

interface Props {
  store: SessionStore;
  // Height in CSS pixels. Width is taken from the container.
  height?: number;
}

// Wraps the three canvas layers and mounts a GanttRenderer bound to `store`.
// React only rerenders on chrome state changes (hover tooltip, live FAB); the
// hot render loop runs outside React entirely.
export function GanttCanvas({ store, height }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const bgRef = useRef<HTMLCanvasElement | null>(null);
  const blocksRef = useRef<HTMLCanvasElement | null>(null);
  const overlayRef = useRef<HTMLCanvasElement | null>(null);

  const [hover, setHover] = useState<HoverState | null>(null);
  const [liveBroken, setLiveBroken] = useState(false);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const themeBase = useThemeStore((s) => s.base);
  const colorBlind = useThemeStore((s) => s.colorBlind);

  const renderer = useMemo(
    () =>
      new GanttRenderer(store, {
        onSelect: (id) => selectSpan(id),
        onHoverChange: (h) => setHover(h),
        onViewportChange: (v) => setLiveBroken(!v.liveFollow),
      }),
    [store, selectSpan],
  );

  // Mount + resize observer.
  useLayoutEffect(() => {
    const bg = bgRef.current;
    const blocks = blocksRef.current;
    const overlay = overlayRef.current;
    const container = containerRef.current;
    if (!bg || !blocks || !overlay || !container) return;
    renderer.attach(bg, blocks, overlay);
    const ro = new ResizeObserver(() => {
      const rect = container.getBoundingClientRect();
      renderer.resize(rect.width, rect.height, window.devicePixelRatio || 1);
    });
    ro.observe(container);
    const rect = container.getBoundingClientRect();
    renderer.resize(rect.width, rect.height, window.devicePixelRatio || 1);
    return () => {
      ro.disconnect();
      renderer.detach();
    };
  }, [renderer]);

  // Theme changes invalidate cached CSS var colors.
  useEffect(() => {
    renderer.onThemeChange();
  }, [renderer, themeBase, colorBlind]);

  // Event forwarding.
  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    e.preventDefault();
    renderer.handleWheel(e.nativeEvent);
  };
  const onClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    renderer.handleClick(e.clientX - r.left, e.clientY - r.top);
  };
  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    renderer.handlePointerMove(e.clientX - r.left, e.clientY - r.top);
  };
  const onLeave = () => {
    renderer.handlePointerLeave();
    setHover(null);
  };

  const span = hover ? store.spans.get(hover.spanId) : undefined;

  return (
    <div
      ref={containerRef}
      className="hg-gantt"
      style={{
        position: 'relative',
        width: '100%',
        height: height ?? '100%',
        overflow: 'hidden',
        background: 'var(--md-sys-color-surface, #10131a)',
        userSelect: 'none',
      }}
      onWheel={onWheel}
      onClick={onClick}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
    >
      <canvas ref={bgRef} style={layer(0)} />
      <canvas ref={blocksRef} style={layer(1)} />
      <canvas ref={overlayRef} style={layer(2)} />
      {hover && span && (
        <div
          role="tooltip"
          style={{
            position: 'absolute',
            left: Math.min(hover.x + 12, 9999),
            top: Math.max(0, hover.y - 48),
            background: 'var(--md-sys-color-surface-container-highest, #31333c)',
            color: 'var(--md-sys-color-on-surface, #e2e2e9)',
            padding: '6px 10px',
            borderRadius: 6,
            pointerEvents: 'none',
            fontSize: 12,
            fontFamily: 'system-ui, sans-serif',
            boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
            zIndex: 10,
            maxWidth: 360,
          }}
        >
          <div style={{ fontWeight: 600 }}>{span.name}</div>
          <div style={{ opacity: 0.8 }}>
            {span.kind} · {span.status}
            {span.endMs !== null && (
              <> · {(span.endMs - span.startMs).toFixed(0)}ms</>
            )}
          </div>
        </div>
      )}
      {liveBroken && !store.nowMs ? null : liveBroken && (
        <button
          onClick={() => {
            renderer.returnToLive();
            setLiveBroken(false);
          }}
          style={{
            position: 'absolute',
            bottom: 16,
            right: 16,
            padding: '8px 16px',
            borderRadius: 999,
            border: 'none',
            background: 'var(--md-sys-color-primary-container, #00468a)',
            color: 'var(--md-sys-color-on-primary-container, #d6e3ff)',
            cursor: 'pointer',
            fontWeight: 600,
            fontSize: 12,
            boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
            zIndex: 10,
          }}
        >
          ⟳ Return to live
        </button>
      )}
    </div>
  );
}

function layer(z: number): React.CSSProperties {
  return {
    position: 'absolute',
    inset: 0,
    width: '100%',
    height: '100%',
    zIndex: z,
    pointerEvents: 'none',
  };
}

// Expose renderer through a ref-less side-channel for stress harness.
export { GanttRenderer };
