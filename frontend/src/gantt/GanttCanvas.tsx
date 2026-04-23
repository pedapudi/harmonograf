import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { GanttRenderer, type DelegationHoverState, type HoverState } from './renderer';
import type { SessionStore } from './index';
import type { ContextWindowSample } from './types';
import { formatTokens } from './contextOverlay';
import { useThemeStore } from '../theme/store';
import { useUiStore } from '../state/uiStore';
import { SpanContextMenu, type ContextMenuState } from '../components/Interaction/SpanContextMenu';
import { usePopoverStore } from '../state/popoverStore';
import { DelegationTooltip } from '../components/DelegationTooltip/DelegationTooltip';
import { actorDisplayLabel } from '../theme/agentColors';

interface ContextHover {
  agentId: string;
  sample: ContextWindowSample;
  ratio: number;
  x: number;
  y: number;
}

interface Props {
  store: SessionStore;
  // Height in CSS pixels. Width is taken from the container.
  height?: number;
  // Overlay layer mounted above the canvas (pins, drag selection, approval
  // editor). Receives the live renderer so children can project session-ms to
  // pixels and re-render on viewport ticks.
  renderOverlay?: (ctx: OverlayContext) => React.ReactNode;
}

export interface OverlayContext {
  renderer: GanttRenderer;
  store: SessionStore;
  widthCss: number;
  heightCss: number;
  // Monotonic counter bumped on every viewport change or resize — used as a
  // React dep by overlay children to recompute layout.
  tick: number;
}

// Wraps the three canvas layers and mounts a GanttRenderer bound to `store`.
// React only rerenders on chrome state changes (hover tooltip, live FAB); the
// hot render loop runs outside React entirely.
export function GanttCanvas({ store, height, renderOverlay }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const bgRef = useRef<HTMLCanvasElement | null>(null);
  const blocksRef = useRef<HTMLCanvasElement | null>(null);
  const overlayRef = useRef<HTMLCanvasElement | null>(null);

  const [hover, setHover] = useState<HoverState | null>(null);
  const [delegHover, setDelegHover] = useState<DelegationHoverState | null>(null);
  const [ctxHover, setCtxHover] = useState<ContextHover | null>(null);
  const [liveBroken, setLiveBroken] = useState(false);
  const [menu, setMenu] = useState<ContextMenuState | null>(null);
  const [overlayTick, setOverlayTick] = useState(0);
  const [canvasSize, setCanvasSize] = useState({ w: 0, h: 0 });
  const openPopover = usePopoverStore((s) => s.openForSpan);
  const closeUnpinnedPopovers = usePopoverStore((s) => s.closeUnpinned);
  const setActiveRenderer = useUiStore((s) => s.setActiveRenderer);
  const showAllAgents = useUiStore((s) => s.showAllAgents);
  const hiddenAgentIds = useUiStore((s) => s.hiddenAgentIds);
  const taskPlanMode = useUiStore((s) => s.taskPlanMode);
  const taskPlanVisible = useUiStore((s) => s.taskPlanVisible);
  const contextOverlayVisible = useUiStore((s) => s.contextOverlayVisible);
  const interventionBandsVisible = useUiStore((s) => s.interventionBandsVisible);
  const selectedTaskId = useUiStore((s) => s.selectedTaskId);
  const themeBase = useThemeStore((s) => s.base);
  const colorBlind = useThemeStore((s) => s.colorBlind);

  const renderer = useMemo(
    () =>
      new GanttRenderer(store, {
        onSelect: (id, cx, cy) => {
          // Click opens a quick-look popover anchored to the span. The full
          // Inspector Drawer is reserved for the "open drawer" action inside
          // the popover (and keyboard nav, notes, etc.).
          if (id) {
            openPopover(id, cx, cy);
            // Reconcile task selection from the clicked span: if any plan
            // task binds to this span id, reflect it as the selected task so
            // the task panel + stages DAG highlight in lockstep.
            const uiState = useUiStore.getState();
            const plans = store.tasks.listPlans();
            let matchTaskId: string | null = null;
            for (const plan of plans) {
              for (const t of plan.tasks) {
                if (t.boundSpanId === id) {
                  matchTaskId = t.id;
                  break;
                }
              }
              if (matchTaskId) break;
            }
            if (uiState.selectedTaskId !== matchTaskId) {
              uiState.selectTask(matchTaskId);
            }
          } else {
            closeUnpinnedPopovers();
          }
        },
        onHoverChange: (h) => setHover(prev => {
          if (prev?.spanId === h?.spanId) return prev;
          return h;
        }),
        onDelegationHoverChange: (h) =>
          setDelegHover((prev) => {
            // Fast path: same edge + negligible pointer motion → bail to
            // avoid a React re-render per mouse event.
            if (!prev && !h) return prev;
            if (prev && h && prev.record.seq === h.record.seq) {
              if (Math.abs(prev.x - h.x) < 2 && Math.abs(prev.y - h.y) < 2) {
                return prev;
              }
            }
            return h;
          }),
        onDelegationClick: (rec) => {
          // Focus the to-agent row so the reader's eye snaps to where the
          // delegated work lands. setFocusedAgent drives row expansion in
          // the renderer via setActiveRenderer; no manual scroll needed
          // because focused rows are tall enough to stand out within the
          // existing viewport.
          const ui = useUiStore.getState();
          ui.setFocusedAgent(rec.toAgentId);
          // Push through the active renderer so the canvas immediately
          // reflects the focus (the renderer subscribes to nothing in
          // uiStore; setFocusedAgent on renderer is an imperative call).
          ui.activeRenderer?.focusAgent(rec.toAgentId);
        },
        onGutterAgentClick: (agentId) => {
          useUiStore.getState().toggleAgentHidden(agentId);
        },
        onViewportChange: (v) => {
          setLiveBroken(!v.liveFollow);
          setOverlayTick((n) => (n + 1) | 0);
          // Sync zoomSeconds so the transport bar label tracks wheel zoom too.
          const state = useUiStore.getState();
          const sec = Math.round(v.windowMs / 1000);
          if (sec !== state.zoomSeconds) {
            state.setZoom(sec);
          }
          // Sync liveFollow so the transport bar Follow button reflects
          // pan/zoom-induced breaks from live mode.
          if (v.liveFollow !== state.liveFollow) {
            useUiStore.setState({ liveFollow: v.liveFollow });
          }
        },
      }),
    [store, openPopover, closeUnpinnedPopovers],
  );

  useEffect(() => {
    setActiveRenderer(renderer);
    return () => setActiveRenderer(null);
  }, [renderer, setActiveRenderer]);

  // Push hidden-agent set to the renderer whenever the UI store changes it.
  useEffect(() => {
    renderer.setHiddenAgents(hiddenAgentIds);
  }, [renderer, hiddenAgentIds]);

  // Push task-plan overlay settings so the canvas hot path sees them without
  // reaching into the zustand store each frame.
  useEffect(() => {
    renderer.setTaskPlanMode(taskPlanMode);
  }, [renderer, taskPlanMode]);
  useEffect(() => {
    renderer.setTaskPlanVisible(taskPlanVisible);
  }, [renderer, taskPlanVisible]);
  useEffect(() => {
    renderer.setContextOverlayVisible(contextOverlayVisible);
  }, [renderer, contextOverlayVisible]);
  useEffect(() => {
    renderer.setInterventionBandsVisible(interventionBandsVisible);
  }, [renderer, interventionBandsVisible]);

  // Resolve the selected task id → its bound span id and push it into the
  // renderer so the overlay draws a halo around the matching bar. Also
  // re-resolve when the tasks collection mutates (bindings arrive async when
  // the planner emits ids before the underlying spans exist).
  useEffect(() => {
    const resolve = (): string | null => {
      if (!selectedTaskId) return null;
      const plans = store.tasks.listPlans();
      for (const plan of plans) {
        for (const t of plan.tasks) {
          if (t.id === selectedTaskId) {
            return t.boundSpanId || null;
          }
        }
      }
      return null;
    };
    renderer.setSelectedTaskSpanId(resolve());
    const unsub = store.tasks.subscribe(() => {
      renderer.setSelectedTaskSpanId(resolve());
    });
    return unsub;
  }, [renderer, store, selectedTaskId]);

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
      setCanvasSize({ w: rect.width, h: rect.height });
      setOverlayTick((n) => (n + 1) | 0);
    });
    ro.observe(container);
    const rect = container.getBoundingClientRect();
    renderer.resize(rect.width, rect.height, window.devicePixelRatio || 1);
    setCanvasSize({ w: rect.width, h: rect.height });
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
    const px = e.clientX - r.left;
    const py = e.clientY - r.top;
    renderer.handlePointerMove(px, py);
    // Cursor: pointer when over anything clickable (span or delegation
    // edge). Matches the implicit behavior spans already got through the
    // overlay hover stroke.
    const el = e.currentTarget;
    if (renderer.spanAt(px, py) || renderer.delegationAt(px, py)) {
      if (el.style.cursor !== 'pointer') el.style.cursor = 'pointer';
    } else if (el.style.cursor === 'pointer') {
      el.style.cursor = '';
    }
    // Only resolve the context-window hover when the pointer is NOT over a
    // span rect — spans own the foreground tooltip, the band is background.
    if (contextOverlayVisible && !renderer.spanAt(px, py)) {
      const cs = renderer.contextSampleAt(px, py);
      if (cs) {
        setCtxHover({
          agentId: cs.agentId,
          sample: cs.sample,
          ratio: cs.ratio,
          x: px,
          y: py,
        });
      } else {
        setCtxHover(null);
      }
    } else {
      setCtxHover(null);
    }
  };
  const onLeave = (e: React.MouseEvent<HTMLDivElement>) => {
    renderer.handlePointerLeave();
    setHover(null);
    setDelegHover(null);
    setCtxHover(null);
    e.currentTarget.style.cursor = '';
  };
  const onContextMenu = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    const r = e.currentTarget.getBoundingClientRect();
    const spanId = renderer.spanAt(e.clientX - r.left, e.clientY - r.top);
    if (!spanId) {
      setMenu(null);
      return;
    }
    setMenu({ spanId, x: e.clientX, y: e.clientY });
  };

  const span = hover ? store.spans.get(hover.spanId) : undefined;

  const resolveDelegationAgentLabel = (agentId: string): string => {
    const synthetic = actorDisplayLabel(agentId);
    if (synthetic) return synthetic;
    return store.agents.get(agentId)?.name ?? agentId;
  };

  return (
    <div
      ref={containerRef}
      className="hg-gantt"
      data-testid="gantt-canvas"
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
      onContextMenu={onContextMenu}
      onMouseMove={onMove}
      onMouseLeave={onLeave}
    >
      <canvas ref={bgRef} style={layer(0)} />
      <canvas ref={blocksRef} style={layer(1)} />
      <canvas ref={overlayRef} style={layer(2)} />
      {!hover && ctxHover && (
        <div
          role="tooltip"
          data-testid="context-window-tooltip"
          style={{
            position: 'absolute',
            left: Math.min(ctxHover.x + 12, 9999),
            top: Math.max(0, ctxHover.y - 52),
            background: 'var(--md-sys-color-surface-container-highest, #31333c)',
            color: 'var(--md-sys-color-on-surface, #e2e2e9)',
            padding: '6px 10px',
            borderRadius: 6,
            pointerEvents: 'none',
            fontSize: 12,
            fontFamily: 'system-ui, sans-serif',
            boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
            zIndex: 9,
            maxWidth: 240,
          }}
        >
          <div style={{ fontWeight: 600 }}>Context window</div>
          <div style={{ opacity: 0.85 }}>
            {formatTokens(ctxHover.sample.tokens)} / {formatTokens(ctxHover.sample.limitTokens)}
            {' · '}
            {Math.round(ctxHover.ratio * 100)}%
          </div>
        </div>
      )}
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
      {delegHover && !hover && (
        <DelegationTooltip
          hover={delegHover}
          resolveAgentLabel={resolveDelegationAgentLabel}
        />
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
      <EmptyWindowHint renderer={renderer} store={store} tick={overlayTick} />
      {/* overlayTick read above so React re-evaluates visibility when the
          renderer's onViewportChange fires — otherwise a post-activity pan
          wouldn't redraw the hint. */}
      {hiddenAgentIds.size > 0 && (
        <button
          onClick={showAllAgents}
          data-testid="show-all-agents"
          style={{
            position: 'absolute',
            left: 8,
            top: 2,
            zIndex: 9,
            padding: '2px 8px',
            fontSize: 10,
            lineHeight: 1.4,
            borderRadius: 999,
            border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
            background: 'var(--md-sys-color-surface-container-high, #262931)',
            color: 'var(--md-sys-color-on-surface, #e2e2e9)',
            cursor: 'pointer',
            pointerEvents: 'auto',
          }}
          title={`Show all ${hiddenAgentIds.size} hidden agent row${hiddenAgentIds.size === 1 ? '' : 's'}`}
        >
          Show all ({hiddenAgentIds.size})
        </button>
      )}
      {menu && <SpanContextMenu state={menu} onClose={() => setMenu(null)} />}
      {renderOverlay &&
        canvasSize.w > 0 &&
        renderOverlay({
          renderer,
          store,
          widthCss: canvasSize.w,
          heightCss: canvasSize.h,
          tick: overlayTick,
        })}
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

// Inline hint shown when the user has panned/zoomed to a window that
// contains no spans on a session with recorded activity. Clicking it slides
// the viewport to the last activity while preserving zoom — the common
// failure mode (#89) is opening a completed session whose default 5-minute
// live window lands past the last span. This also serves as a safety net
// for that same shape on a live session where the user pans into the
// future.
interface EmptyWindowHintProps {
  renderer: GanttRenderer;
  store: SessionStore;
  tick: number;
}
function EmptyWindowHint({ renderer, store, tick }: EmptyWindowHintProps) {
  // Read through renderer state rather than holding our own — the renderer
  // is the source of truth and tick ensures we re-evaluate on viewport
  // changes. `tick` is load-bearing here: React wouldn't otherwise notice
  // that getViewport() has returned something new.
  void tick;
  const v = renderer.getViewport();
  const maxEnd = store.spans.maxEndMs();
  // Gate: only show when there's recorded activity, we're not in live
  // follow (so not actively tracking a cursor that will catch up), and
  // the viewport left edge sits past the last activity. The right-edge
  // margin keeps the hint from flickering on/off during drag-zoom at the
  // boundary.
  if (maxEnd <= 0) return null;
  if (v.liveFollow) return null;
  const vs = v.endMs - v.windowMs;
  const beyondEnd = vs > maxEnd;
  if (!beyondEnd) return null;
  return (
    <button
      data-testid="gantt-empty-window-hint"
      onClick={() => renderer.jumpToLastActivity()}
      style={{
        position: 'absolute',
        top: '50%',
        left: '50%',
        transform: 'translate(-50%, -50%)',
        padding: '10px 18px',
        borderRadius: 999,
        border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
        background: 'var(--md-sys-color-surface-container-high, #262931)',
        color: 'var(--md-sys-color-on-surface, #e2e2e9)',
        cursor: 'pointer',
        fontSize: 13,
        fontWeight: 500,
        boxShadow: '0 4px 16px rgba(0,0,0,0.4)',
        zIndex: 10,
      }}
      title="Jump to the last recorded activity in this session"
    >
      No activity in this window &middot; Jump to last activity
    </button>
  );
}

// Expose renderer through a ref-less side-channel for stress harness.
export { GanttRenderer };
