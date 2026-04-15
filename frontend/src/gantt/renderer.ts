// Three-layer canvas Gantt renderer.
//
// Layers (all share the same dimensions, stacked z-index in the DOM):
//   0 background — rows, gridlines, time axis, agent gutter. Redraws on
//     viewport change, agent list change, or theme change.
//   1 blocks     — span rectangles. Redraws on viewport change or when a
//     dirty rect intersects the viewport.
//   2 overlay    — hover, selection, cursor, breathing/pulse animations. If
//     any animated span is in view, this layer redraws every frame; otherwise
//     it redraws only on interaction.
//
// React NEVER drives the render loop. GanttCanvas.tsx attaches the three
// canvases to a Renderer instance; the instance subscribes to the SessionStore
// and schedules redraws via requestAnimationFrame.

import { colorForAgent } from '../theme/agentColors';
import type { TaskPlanMode } from '../state/uiStore';
import { bucketKey, cssVar, refreshThemeCache, resolveStyle } from './colors';
import {
  computeContextBandGeom,
  contextColorForRatio,
  contextRatio,
} from './contextOverlay';
import type { SessionStore } from './index';
import type { SpanIndex, DirtyRect } from './spatialIndex';
import type { Agent, ContextWindowSample, Span, SpanKind, Task, TaskStatus } from './types';
import {
  GUTTER_WIDTH_PX,
  ROW_HEIGHT_FOCUSED_PX,
  ROW_HEIGHT_PX,
  SUB_LANE_HEIGHT_PX,
  TOP_MARGIN_PX,
  ZOOM_MAX_MS,
  ZOOM_MIN_MS,
  advanceLive,
  defaultViewport,
  msToPx,
  pan,
  pxToMs,
  returnToLive,
  viewportStart,
  zoomAround,
  type ViewportState,
} from './viewport';

const MIN_BLOCK_WIDTH_PX = 2;

export interface RendererCallbacks {
  onSelect?: (spanId: string | null, clickX: number, clickY: number) => void;
  onHoverChange?: (hover: HoverState | null) => void;
  onViewportChange?: (v: ViewportState) => void;
  // Fired when the user clicks inside the agent gutter on a specific agent
  // row. Used by the DOM layer to toggle agent row visibility.
  onGutterAgentClick?: (agentId: string) => void;
}

// Collapsed height used for hidden agents — tall enough to stay clickable and
// readable (small italic "hidden" label) but short enough to reclaim vertical
// space when many agents are filtered.
const ROW_HEIGHT_COLLAPSED_PX = 18;

export interface HoverState {
  spanId: string;
  // Tooltip anchor in CSS pixels, relative to the canvas container.
  x: number;
  y: number;
}

// Cached layout for a single LINK_INVOKED edge, rebuilt each blocks redraw.
// Persisted so overlay hover highlight and edge hit-testing don't re-walk the
// span index per frame.
interface EdgeLayout {
  sourceSpanId: string;
  targetSpanId: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  color: string;
}

interface FrameMetrics {
  lastFrameMs: number;
  // Rolling window of recent frame durations for p95 calculation in stress mode.
  samples: Float32Array;
  sampleIdx: number;
  sampleCount: number;
}

// Icons drawn when a block is wide enough. Single-char glyphs keep the fast
// path cheap — no font atlas needed at 11sp.
const KIND_ICON: Record<SpanKind, string> = {
  INVOCATION: '◉',
  LLM_CALL: '✦',
  TOOL_CALL: '⚙',
  USER_MESSAGE: '👤',
  AGENT_MESSAGE: '💬',
  TRANSFER: '↪',
  WAIT_FOR_HUMAN: '⏸',
  PLANNED: '◌',
  CUSTOM: '•',
};

export class GanttRenderer {
  private bg: HTMLCanvasElement | null = null;
  private blocks: HTMLCanvasElement | null = null;
  private overlay: HTMLCanvasElement | null = null;
  private bgCtx: CanvasRenderingContext2D | null = null;
  private blocksCtx: CanvasRenderingContext2D | null = null;
  private overlayCtx: CanvasRenderingContext2D | null = null;

  private widthCss = 0;
  private heightCss = 0;

  private viewport: ViewportState = defaultViewport();
  private focusedAgentId: string | null = null;
  private hiddenAgentIds: Set<string> = new Set();
  private selectedSpanId: string | null = null;
  // Span id resolved from the UI store's `selectedTaskId` — set by chrome via
  // setSelectedTaskSpanId so the overlay can draw a bright halo around the bar
  // that corresponds to the currently selected task. Kept separate from
  // `selectedSpanId` so a direct click-to-select (thin stroke) and a
  // task-driven selection (halo + stroke) can coexist and emphasize differently.
  private selectedTaskSpanId: string | null = null;
  private hoveredSpanId: string | null = null;
  private edges: EdgeLayout[] = [];
  private hoveredEdgeIdx: number | null = null;

  // Dirty flags per layer.
  private bgDirty = true;
  private blocksDirty = true;
  private overlayDirty = true;

  // Running frame loop handle.
  private rafHandle = 0;
  private unsub: Array<() => void> = [];
  private stopped = true;

  // Perf instrumentation for the stress harness.
  metrics: FrameMetrics = {
    lastFrameMs: 0,
    samples: new Float32Array(600),
    sampleIdx: 0,
    sampleCount: 0,
  };

  // When non-null, the renderer freezes "session-relative now" at this value
  // instead of advancing from the wall clock. Set via freezeAt().
  private _frozenNowMs: number | null = null;

  // Task-plan overlay state, pushed from the UI store via setters.
  private taskPlanMode: TaskPlanMode = 'pre-strip';
  private taskPlanVisible = true;

  // Context-window overlay visibility, pushed from the UI store. The band
  // is drawn inside drawBlocks so flipping this only re-renders the blocks
  // layer, not the background or the overlay cursor.
  private contextOverlayVisible = true;

  // Cached per-agent band geometry from the last drawBlocks pass. Used by
  // the hover tooltip code to interpolate the exact sample at the pointer x
  // without re-walking the sample array.
  private contextBandByAgent = new Map<
    string,
    {
      samples: readonly ContextWindowSample[];
      bandTopYAtRatio1: number;
      baselineY: number;
    }
  >();

  // Cache of on-screen task chip rects, populated during chip draw and read
  // by the dependency-edge pass. Lives on the instance so the two passes can
  // straddle the span render (chips under spans, dep arrows above).
  private taskRectsById = new Map<string, { x: number; y: number; w: number; h: number }>();

  private store: SessionStore;
  private cb: RendererCallbacks;

  constructor(store: SessionStore, cb: RendererCallbacks = {}) {
    this.store = store;
    this.cb = cb;
  }

  attach(
    bg: HTMLCanvasElement,
    blocks: HTMLCanvasElement,
    overlay: HTMLCanvasElement,
  ): void {
    this.bg = bg;
    this.blocks = blocks;
    this.overlay = overlay;
    this.bgCtx = bg.getContext('2d', { alpha: false });
    this.blocksCtx = blocks.getContext('2d', { alpha: true });
    this.overlayCtx = overlay.getContext('2d', { alpha: true });
    this.stopped = false;
    this.unsub.push(
      this.store.spans.subscribe((d) => this.onSpanDirty(d)),
      this.store.agents.subscribe(() => {
        this.bgDirty = true;
        this.blocksDirty = true;
      }),
      this.store.tasks.subscribe(() => {
        // Task plan changes redraw blocks (task chips live alongside spans).
        this.blocksDirty = true;
      }),
      this.store.contextSeries.subscribe(() => {
        // A new context-window sample always redraws blocks (band geometry
        // + header chip both live in the blocks pass).
        this.blocksDirty = true;
      }),
    );
    this.scheduleFrame();
  }

  detach(): void {
    this.stopped = true;
    if (this.rafHandle) cancelAnimationFrame(this.rafHandle);
    this.rafHandle = 0;
    for (const fn of this.unsub) fn();
    this.unsub = [];
    this.bg = this.blocks = this.overlay = null;
    this.bgCtx = this.blocksCtx = this.overlayCtx = null;
  }

  resize(widthCss: number, heightCss: number, dpr: number): void {
    this.widthCss = widthCss;
    this.heightCss = heightCss;
    const w = Math.round(widthCss * dpr);
    const h = Math.round(heightCss * dpr);
    for (const c of [this.bg, this.blocks, this.overlay]) {
      if (!c) continue;
      if (c.width !== w) c.width = w;
      if (c.height !== h) c.height = h;
      c.style.width = widthCss + 'px';
      c.style.height = heightCss + 'px';
    }
    for (const ctx of [this.bgCtx, this.blocksCtx, this.overlayCtx]) {
      if (!ctx) continue;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    this.bgDirty = true;
    this.blocksDirty = true;
    this.overlayDirty = true;
  }

  onThemeChange(): void {
    refreshThemeCache();
    this.bgDirty = true;
    this.blocksDirty = true;
    this.overlayDirty = true;
  }

  getViewport(): ViewportState {
    return this.viewport;
  }

  setViewport(v: ViewportState): void {
    this.viewport = v;
    this.bgDirty = true;
    this.blocksDirty = true;
    this.overlayDirty = true;
    this.cb.onViewportChange?.(v);
  }

  // --- Interaction -------------------------------------------------------

  // Forwarded by GanttCanvas from DOM events.
  handleWheel(ev: WheelEvent): void {
    if (ev.ctrlKey || ev.metaKey || Math.abs(ev.deltaY) > Math.abs(ev.deltaX)) {
      // Zoom
      const focusMs = pxToMs(this.viewport, this.widthCss, ev.offsetX);
      const factor = Math.exp(-ev.deltaY * 0.001);
      this.setViewport(zoomAround(this.viewport, focusMs, factor));
    } else {
      // Horizontal pan
      const frac = ev.deltaX / (this.widthCss - GUTTER_WIDTH_PX);
      this.setViewport(pan(this.viewport, frac));
    }
  }

  handleClick(x: number, y: number): void {
    // Gutter clicks target the agent row header — forward to the DOM layer so
    // it can toggle the agent's visibility through the UI store.
    if (x < GUTTER_WIDTH_PX && y >= TOP_MARGIN_PX) {
      const agentId = this.agentAtY(y);
      if (agentId) this.cb.onGutterAgentClick?.(agentId);
      return;
    }
    const hit = this.hitTest(x, y);
    this.selectedSpanId = hit;
    this.overlayDirty = true;
    this.cb.onSelect?.(hit, x, y);
  }

  private agentAtY(py: number): string | null {
    const agents = this.store.agents.list;
    let y = TOP_MARGIN_PX;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      if (py >= y && py < y + rowH) return agent.id;
      y += rowH;
    }
    return null;
  }

  // Public hit-test for DOM overlays that need to know what span sits under
  // the pointer (right-click context menu, annotation pin targeting).
  spanAt(x: number, y: number): string | null {
    return this.hitTest(x, y);
  }

  // Public accessor: used by DOM-layer overlays (SpanPopover) to anchor
  // elements to a span's current on-canvas rectangle.
  rectFor(spanId: string): { x: number; y: number; w: number; h: number } | null {
    return this.rectForSpan(spanId);
  }

  handlePointerMove(x: number, y: number): void {
    const hit = this.hitTest(x, y);
    if (hit !== this.hoveredSpanId) {
      this.hoveredSpanId = hit;
      this.overlayDirty = true;
      this.cb.onHoverChange?.(hit ? { spanId: hit, x, y } : null);
    }
    // Edge hover: only when no span is under the cursor, so spans win.
    const edgeIdx = hit ? null : this.edgeHitTest(x, y);
    if (edgeIdx !== this.hoveredEdgeIdx) {
      this.hoveredEdgeIdx = edgeIdx;
      this.overlayDirty = true;
    }
  }

  handlePointerLeave(): void {
    if (this.hoveredSpanId) {
      this.hoveredSpanId = null;
      this.overlayDirty = true;
      this.cb.onHoverChange?.(null);
    }
    if (this.hoveredEdgeIdx !== null) {
      this.hoveredEdgeIdx = null;
      this.overlayDirty = true;
    }
  }

  panBy(fraction: number): void {
    this.setViewport(pan(this.viewport, fraction));
  }

  zoomBy(factor: number): void {
    const mid = viewportStart(this.viewport) + this.viewport.windowMs / 2;
    this.setViewport(zoomAround(this.viewport, mid, factor));
  }

  returnToLive(): void {
    this.setViewport(returnToLive(this.viewport, this.store.nowMs));
  }

  setLiveFollow(enabled: boolean): void {
    if (enabled) {
      this.returnToLive();
    } else if (this.viewport.liveFollow) {
      this.setViewport({ ...this.viewport, liveFollow: false });
    }
  }

  fitAll(): void {
    const maxEnd = Math.max(this.store.spans.maxEndMs(), this.store.nowMs, 1);
    const window = Math.min(ZOOM_MAX_MS, Math.max(ZOOM_MIN_MS, maxEnd * 1.05));
    // Clamp so the left edge never sits before session start (t=0).
    const endMs = Math.max(window, maxEnd);
    this.setViewport({
      ...this.viewport,
      endMs,
      windowMs: window,
      liveFollow: false,
    });
  }

  focusAgent(agentId: string | null): void {
    this.focusedAgentId = agentId;
    this.bgDirty = true;
    this.blocksDirty = true;
  }

  // Row layout the renderer currently uses. Exposed so DOM overlays
  // (GanttDomProxy, PinStrip, RangeSelectionLayer, ApprovalEditor) can mirror
  // the canvas exactly — honoring focus state AND hidden agent collapse — in
  // a single place instead of each duplicating the math.
  getRowLayout(): Array<{ agentId: string; top: number; height: number; hidden: boolean }> {
    const out: Array<{ agentId: string; top: number; height: number; hidden: boolean }> = [];
    let y = TOP_MARGIN_PX;
    for (const agent of this.store.agents.list) {
      const h = this.rowHeight(agent.id);
      out.push({ agentId: agent.id, top: y, height: h, hidden: this.hiddenAgentIds.has(agent.id) });
      y += h;
    }
    return out;
  }

  isAgentHidden(agentId: string): boolean {
    return this.hiddenAgentIds.has(agentId);
  }

  setTaskPlanMode(m: TaskPlanMode): void {
    if (this.taskPlanMode === m) return;
    this.taskPlanMode = m;
    this.blocksDirty = true;
  }

  setTaskPlanVisible(v: boolean): void {
    if (this.taskPlanVisible === v) return;
    this.taskPlanVisible = v;
    this.blocksDirty = true;
  }

  setContextOverlayVisible(v: boolean): void {
    if (this.contextOverlayVisible === v) return;
    this.contextOverlayVisible = v;
    this.blocksDirty = true;
    // Clear cached geometry when the overlay is hidden so the hover tooltip
    // stops reporting stale samples.
    if (!v) this.contextBandByAgent.clear();
  }

  // Push the resolved span id for the currently selected task (if any). The
  // renderer doesn't know about tasks directly — chrome resolves taskId →
  // boundSpanId via the TaskRegistry and calls this. Changing only marks the
  // overlay dirty so the hot block path isn't touched.
  setSelectedTaskSpanId(spanId: string | null): void {
    if (this.selectedTaskSpanId === spanId) return;
    this.selectedTaskSpanId = spanId;
    this.overlayDirty = true;
  }

  setHiddenAgents(ids: Iterable<string>): void {
    const next = new Set(ids);
    if (
      next.size === this.hiddenAgentIds.size &&
      [...next].every((id) => this.hiddenAgentIds.has(id))
    ) {
      return;
    }
    this.hiddenAgentIds = next;
    this.bgDirty = true;
    this.blocksDirty = true;
    this.overlayDirty = true;
  }

  // --- Frame loop --------------------------------------------------------

  private onSpanDirty(d: DirtyRect): void {
    const vs = viewportStart(this.viewport);
    const ve = this.viewport.endMs;
    // Coalesce: any touch within the viewport is a blocks redraw next frame.
    if (d.agentId === null || (d.t1 >= vs && d.t0 <= ve)) {
      this.blocksDirty = true;
    }
  }

  private scheduleFrame(): void {
    if (this.stopped || this.rafHandle) return;
    this.rafHandle = requestAnimationFrame(() => {
      this.rafHandle = 0;
      const start = performance.now();
      this.frame();
      const dur = performance.now() - start;
      this.metrics.lastFrameMs = dur;
      const i = this.metrics.sampleIdx;
      this.metrics.samples[i] = dur;
      this.metrics.sampleIdx = (i + 1) % this.metrics.samples.length;
      if (this.metrics.sampleCount < this.metrics.samples.length) {
        this.metrics.sampleCount++;
      }
      if (!this.stopped) this.scheduleFrame();
    });
  }

  // --- Freeze / unfreeze ----------------------------------------------------

  /**
   * Freeze the renderer's "session-relative now" so open spans stop growing
   * and the live-edge cursor stops moving.
   * - Pass a number to freeze at a specific session-relative timestamp.
   * - Pass null to return to live mode (advance from wall clock each frame).
   * When called with a number, if you want to freeze at the current live edge,
   * pass the current store.nowMs; the renderer's public getNowMs() returns it.
   */
  public freezeAt(ts: number | null): void {
    if (ts !== null) {
      // Freeze at the provided session-relative timestamp (or current nowMs if
      // caller passed the sentinel value we expose via getNowMs()).
      this._frozenNowMs = ts;
      this.store.nowMs = ts;
    } else {
      // Unfreeze — resume advancing from wall clock next frame.
      this._frozenNowMs = null;
    }
    this.blocksDirty = true;
    this.overlayDirty = true;
  }

  /** Returns the current session-relative "now" in ms. */
  public getNowMs(): number {
    return this.store.nowMs;
  }

  private frame(): void {
    // Advance "session-relative now" from the wall clock. The renderer owns
    // this — transport only sets wallClockStartMs on session connect.
    // When frozen, skip the advance so bars and the live cursor stand still.
    const prevNowMs = this.store.nowMs;
    if (this._frozenNowMs !== null) {
      // Frozen — keep store.nowMs at the frozen value (already set in freezeAt).
    } else if (this.store.wallClockStartMs > 0) {
      this.store.nowMs = Date.now() - this.store.wallClockStartMs;
    }

    // Live follow advance
    this.viewport = advanceLive(this.viewport, this.store.nowMs);

    // When nowMs advances, redraw blocks (bar growth depends on
    // `s.endMs ?? nowMs`) and overlay (the "now" cursor line moves).
    if (this.store.nowMs !== prevNowMs) {
      this.blocksDirty = true;
      this.overlayDirty = true;
    }

    if (this.bgDirty) this.drawBackground();
    if (this.blocksDirty) this.drawBlocks();
    if (this.overlayDirty) this.drawOverlay();
    this.bgDirty = this.blocksDirty = this.overlayDirty = false;
  }

  // --- Background --------------------------------------------------------

  private drawBackground(): void {
    const ctx = this.bgCtx;
    if (!ctx) return;
    const w = this.widthCss;
    const h = this.heightCss;
    ctx.fillStyle = cssVar('--md-sys-color-surface') || '#10131a';
    ctx.fillRect(0, 0, w, h);

    this.drawTimeAxis(ctx);
    this.drawRows(ctx);
    this.drawGutter(ctx);
  }

  private drawTimeAxis(ctx: CanvasRenderingContext2D): void {
    const w = this.widthCss;
    const vs = viewportStart(this.viewport);
    const ve = this.viewport.endMs;
    const winMs = this.viewport.windowMs;
    const tickMs = pickTickMs(winMs);
    ctx.strokeStyle = cssVar('--md-sys-color-outline-variant') || '#43474e';
    ctx.fillStyle = cssVar('--md-sys-color-on-surface-variant') || '#c3c6cf';
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 1;
    const t0Tick = Math.ceil(vs / tickMs) * tickMs;
    ctx.beginPath();
    for (let t = t0Tick; t <= ve; t += tickMs) {
      const x = Math.floor(msToPx(this.viewport, w, t)) + 0.5;
      if (x < GUTTER_WIDTH_PX) continue;
      ctx.moveTo(x, 0);
      ctx.lineTo(x, this.heightCss);
      ctx.fillText(formatTickLabel(t, tickMs), x + 4, TOP_MARGIN_PX / 2);
    }
    ctx.stroke();

    // Top margin separator line.
    ctx.beginPath();
    ctx.moveTo(GUTTER_WIDTH_PX, TOP_MARGIN_PX + 0.5);
    ctx.lineTo(w, TOP_MARGIN_PX + 0.5);
    ctx.stroke();
  }

  private drawRows(ctx: CanvasRenderingContext2D): void {
    const agents = this.store.agents.list;
    const w = this.widthCss;
    ctx.strokeStyle = cssVar('--md-sys-color-outline-variant') || '#43474e';
    ctx.lineWidth = 1;
    let y = TOP_MARGIN_PX;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      // Row separator
      ctx.beginPath();
      ctx.moveTo(GUTTER_WIDTH_PX, y + rowH + 0.5);
      ctx.lineTo(w, y + rowH + 0.5);
      ctx.stroke();
      // Agent color strip on the left edge of the data area (8px).
      ctx.fillStyle = colorForAgent(agent.id);
      ctx.fillRect(GUTTER_WIDTH_PX, y, 8, rowH);
      y += rowH;
    }
  }

  private drawGutter(ctx: CanvasRenderingContext2D): void {
    const agents = this.store.agents.list;
    ctx.fillStyle = cssVar('--md-sys-color-surface-container') || '#1c1f26';
    ctx.fillRect(0, 0, GUTTER_WIDTH_PX, this.heightCss);
    ctx.strokeStyle = cssVar('--md-sys-color-outline-variant') || '#43474e';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(GUTTER_WIDTH_PX - 0.5, 0);
    ctx.lineTo(GUTTER_WIDTH_PX - 0.5, this.heightCss);
    ctx.stroke();

    // Column header label for the gutter.
    ctx.fillStyle = cssVar('--md-sys-color-on-surface-variant') || '#c3c6cf';
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.fillText('Agents', 12, TOP_MARGIN_PX / 2);

    let y = TOP_MARGIN_PX;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      this.drawAgentRowHeader(ctx, agent, y, rowH);
      y += rowH;
    }
  }

  private drawAgentRowHeader(
    ctx: CanvasRenderingContext2D,
    agent: Agent,
    y: number,
    rowH: number,
  ): void {
    const hidden = this.hiddenAgentIds.has(agent.id);

    // Color chip — smaller and vertically centered in collapsed rows.
    const chipSize = hidden ? 8 : 12;
    const chipX = 12;
    const chipY = y + rowH / 2 - chipSize / 2;
    ctx.fillStyle = colorForAgent(agent.id);
    roundRectPath(ctx, chipX, chipY, chipSize, chipSize, 3);
    ctx.fill();

    const textX = chipX + 18;
    const maxNameW = GUTTER_WIDTH_PX - 80;

    if (hidden) {
      // Single-line collapsed layout.
      ctx.fillStyle = cssVar('--md-sys-color-on-surface-variant') || '#c3c6cf';
      ctx.font = 'italic 11px system-ui, sans-serif';
      ctx.textBaseline = 'middle';
      const name = agent.name || agent.id;
      ctx.fillText(`${truncate(ctx, name, maxNameW - 36)} · hidden`, textX, y + rowH / 2);
      // Status dot still rendered below.
    } else {
      // Name
      ctx.fillStyle = cssVar('--md-sys-color-on-surface') || '#e2e2e9';
      ctx.font = '600 13px system-ui, sans-serif';
      ctx.textBaseline = 'middle';
      const name = agent.name || agent.id;
      ctx.fillText(truncate(ctx, name, maxNameW), textX, y + rowH / 2 - 7);

      // Framework badge
      ctx.font = '10px system-ui, sans-serif';
      ctx.fillStyle = cssVar('--md-sys-color-on-surface-variant') || '#c3c6cf';
      ctx.fillText(agent.framework, textX, y + rowH / 2 + 8);
    }

    // Connection status dot
    const dotX = GUTTER_WIDTH_PX - 18;
    const dotY = y + rowH / 2;
    ctx.beginPath();
    ctx.arc(dotX, dotY, 5, 0, Math.PI * 2);
    ctx.fillStyle =
      agent.status === 'CONNECTED'
        ? '#4caf50'
        : agent.status === 'DISCONNECTED'
          ? cssVar('--md-sys-color-outline') || '#8d9199'
          : cssVar('--md-sys-color-error') || '#ffb4ab';
    ctx.fill();
  }

  // --- Blocks ------------------------------------------------------------

  private drawBlocks(): void {
    const ctx = this.blocksCtx;
    if (!ctx) return;
    const w = this.widthCss;
    const h = this.heightCss;
    ctx.clearRect(0, 0, w, h);
    // Clip the data area so blocks never overdraw the gutter or top axis.
    ctx.save();
    ctx.beginPath();
    ctx.rect(GUTTER_WIDTH_PX, TOP_MARGIN_PX, w - GUTTER_WIDTH_PX, h - TOP_MARGIN_PX);
    ctx.clip();

    const vs = viewportStart(this.viewport);
    const ve = this.viewport.endMs;
    const agents = this.store.agents.list;
    const spanIndex: SpanIndex = this.store.spans;

    // Context-window overlay — drawn FIRST inside the clipped data area so
    // every downstream pass (task chips, spans, link curves) paints on top.
    // It's a background signal, not foreground noise.
    this.drawContextWindowBands(ctx);

    // Draw task-plan chips + dependency edges BEFORE spans so spans visually
    // overlay the planned chips. Task chips participate in the same frame as
    // spans — no parallel React loop. See AGENTS.md: canvas hot path only.
    this.drawTasksAndEdges(ctx);

    // Gather + bucket.
    type Bucket = {
      fill: string;
      opacity: number;
      hatched: boolean;
      dashed: boolean;
      rects: number[]; // flat [x,y,wr,hr,...]
      labels: Array<{ s: Span; x: number; y: number; w: number; h: number }>;
    };
    const buckets = new Map<string, Bucket>();
    // Liveness ticks drawn on top of running LLM spans. Each entry is the
    // geometry of the span plus the streaming tick counter from the client.
    // Task #12: thinking progress indicator for in-flight LLM calls.
    const tickOverlays: Array<{
      x: number;
      y: number;
      w: number;
      h: number;
      ticks: number;
    }> = [];
    // Task #4: small brain badge painted in the corner of LLM_CALL blocks
    // whose spans carry reasoning content. The badge is a hint that the
    // span has a Trajectory worth opening in the drawer.
    const brainBadges: Array<{ x: number; y: number; w: number; h: number }> = [];

    let y = TOP_MARGIN_PX;
    let visibleCount = 0;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      if (this.hiddenAgentIds.has(agent.id)) {
        y += rowH;
        continue;
      }
      const rowDataX = GUTTER_WIDTH_PX + 10; // skip color strip
      const scratch: Span[] = [];
      spanIndex.queryAgent(agent.id, vs, ve, scratch);
      // Density merge for sub-pixel blocks. We coalesce within each lane.
      const laneDensity = new Map<number, { x1: number; x2: number; count: number; y: number; h: number }>();
      for (const s of scratch) {
        const x1 = Math.max(
          rowDataX,
          msToPx(this.viewport, w, s.startMs),
        );
        const x2 = msToPx(this.viewport, w, s.endMs ?? this.store.nowMs);
        const width = Math.max(MIN_BLOCK_WIDTH_PX, x2 - x1);
        const laneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(rowH / 3));
        const laneTop = y + 2 + (s.lane >= 0 ? s.lane : 0) * laneH;
        const laneBot = Math.min(y + rowH - 2, laneTop + laneH - 2);
        const rectH = Math.max(6, laneBot - laneTop);
        if (width < MIN_BLOCK_WIDTH_PX * 2 && s.status !== 'AWAITING_HUMAN') {
          // Merge into a density stripe for this lane.
          const d = laneDensity.get(s.lane >= 0 ? s.lane : 0);
          if (d && x1 <= d.x2 + 1) {
            d.x2 = Math.max(d.x2, x1 + width);
            d.count++;
            continue;
          }
          laneDensity.set(s.lane >= 0 ? s.lane : 0, {
            x1,
            x2: x1 + width,
            count: 1,
            y: laneTop,
            h: rectH,
          });
          continue;
        }
        const style = resolveStyle(s.kind, s.status, s.replaced);
        const key = bucketKey(style);
        let b = buckets.get(key);
        if (!b) {
          b = {
            fill: style.fill,
            opacity: style.opacity,
            hatched: style.hatched,
            dashed: style.dashed,
            rects: [],
            labels: [],
          };
          buckets.set(key, b);
        }
        b.rects.push(x1, laneTop, width, rectH);
        if (width > 12) {
          b.labels.push({ s, x: x1, y: laneTop, w: width, h: rectH });
        }
        if (
          s.status === 'RUNNING' &&
          s.kind === 'LLM_CALL' &&
          width >= 8
        ) {
          const tickAttr = s.attributes['streaming_tick'];
          const ticks =
            tickAttr && tickAttr.kind === 'int'
              ? Number(tickAttr.value)
              : tickAttr && tickAttr.kind === 'double'
                ? tickAttr.value
                : 0;
          if (ticks > 0) {
            tickOverlays.push({ x: x1, y: laneTop, w: width, h: rectH, ticks });
          }
        }
        // Brain badge: any LLM_CALL with reasoning content (live or done).
        // Gate on has_thinking=true to keep the test cheap (no string attr
        // parse in the hot path) and skip narrow blocks where the badge
        // would collide with the kind icon label.
        if (s.kind === 'LLM_CALL' && width >= 14) {
          const ht = s.attributes['has_thinking'];
          if (ht && ht.kind === 'bool' && ht.value) {
            brainBadges.push({ x: x1, y: laneTop, w: width, h: rectH });
          }
        }
        visibleCount++;
      }
      // Flush density stripes as a neutral color band with a count annotation.
      ctx.fillStyle = cssVar('--md-sys-color-outline') || '#8d9199';
      for (const d of laneDensity.values()) {
        ctx.globalAlpha = 0.6;
        ctx.fillRect(d.x1, d.y, Math.max(2, d.x2 - d.x1), d.h);
      }
      ctx.globalAlpha = 1;
      y += rowH;
    }

    // Flush buckets (single fillRect batch per color bucket — doc 04 §9.1).
    for (const b of buckets.values()) {
      ctx.globalAlpha = b.opacity;
      ctx.fillStyle = b.fill;
      const rects = b.rects;
      for (let i = 0; i < rects.length; i += 4) {
        // Rounded rects are expensive; skip the path when block is small.
        const bw = rects[i + 2];
        if (bw < 6) {
          ctx.fillRect(rects[i], rects[i + 1], bw, rects[i + 3]);
        } else {
          ctx.beginPath();
          roundRectPath(ctx, rects[i], rects[i + 1], bw, rects[i + 3], 4);
          ctx.fill();
        }
      }
      if (b.dashed) {
        ctx.strokeStyle = b.fill;
        ctx.globalAlpha = Math.min(1, b.opacity + 0.3);
        ctx.setLineDash([4, 3]);
        ctx.lineWidth = 1;
        for (let i = 0; i < rects.length; i += 4) {
          ctx.strokeRect(rects[i] + 0.5, rects[i + 1] + 0.5, rects[i + 2], rects[i + 3]);
        }
        ctx.setLineDash([]);
      }
      ctx.globalAlpha = 1;
    }

    // Labels pass — full label for bars > 40px, icon-only for 12–40px.
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = cssVar('--md-sys-color-on-surface') || '#e2e2e9';
    for (const b of buckets.values()) {
      for (const l of b.labels) {
        const { s, x, y: ly, w: lw, h: lh } = l;
        const icon = KIND_ICON[s.kind];
        if (lw > 40) {
          const label = icon ? `${icon} ${s.name}` : s.name;
          ctx.save();
          ctx.beginPath();
          ctx.rect(x, ly, lw, lh);
          ctx.clip();
          ctx.fillText(label, x + 4, ly + lh / 2);
          ctx.restore();
        } else if (icon) {
          // Narrow bar: just the kind icon centered
          ctx.fillText(icon, x + lw / 2, ly + lh / 2);
        }
      }
    }

    // Liveness ticks: small vertical marks at the trailing edge of running
    // LLM spans, one per partial streaming event. Gives the user a heartbeat
    // that the model is still thinking even while the bar grows. Task #12.
    if (tickOverlays.length > 0) {
      ctx.fillStyle = cssVar('--md-sys-color-on-primary') || '#ffffff';
      ctx.globalAlpha = 0.65;
      for (const o of tickOverlays) {
        const nTicks = Math.min(o.ticks, Math.max(1, Math.floor(o.w / 4)));
        const step = o.w / (nTicks + 1);
        const tickH = Math.max(2, Math.floor(o.h / 2));
        const tickY = o.y + Math.floor((o.h - tickH) / 2);
        for (let k = 1; k <= nTicks; k++) {
          const tx = o.x + step * k;
          ctx.fillRect(Math.floor(tx), tickY, 1, tickH);
        }
      }
      ctx.globalAlpha = 1;
    }

    // Brain badges: 10x10 corner glyphs painted in the top-right corner of
    // LLM_CALL blocks that carry thinking. Drawn after labels so they stay
    // visible and crisp. Task #4.
    if (brainBadges.length > 0) {
      ctx.save();
      ctx.font = '10px system-ui, sans-serif';
      ctx.textBaseline = 'top';
      ctx.textAlign = 'right';
      ctx.fillStyle = cssVar('--md-sys-color-primary') || '#a8c8ff';
      ctx.globalAlpha = 0.92;
      for (const b of brainBadges) {
        ctx.fillText('🧠', b.x + b.w - 2, b.y + 1);
      }
      ctx.globalAlpha = 1;
      ctx.restore();
    }

    // Task-plan dependency edges are drawn after the span bucket flush so
    // their curves + arrowheads render above spans (chips themselves were
    // laid down earlier in drawTasksAndEdges, before spans, on purpose).
    this.drawTaskDepEdges(ctx);

    // Links layer: cross-agent LINK_INVOKED edges. Drawn inside the clipped
    // data area so curves never bleed into the gutter or axis.
    this.drawLinks(ctx);

    ctx.restore();
    // Expose last-draw count for stress tooling.
    this.lastDrawnCount = visibleCount;
    this.lastBrainBadgeCount = brainBadges.length;
  }

  // --- Links -------------------------------------------------------------

  private drawLinks(ctx: CanvasRenderingContext2D): void {
    this.edges.length = 0;
    const vs = viewportStart(this.viewport);
    const ve = this.viewport.endMs;
    const w = this.widthCss;
    const agents = this.store.agents.list;
    if (agents.length === 0) return;

    // Row layout lookup: top, height, center. We need top+height to resolve
    // per-span lane positions so link anchors land on the actual bar, not the
    // row's center line.
    type RowLayout = { top: number; height: number; centerY: number };
    const rowLayout = new Map<string, RowLayout>();
    let y = TOP_MARGIN_PX;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      rowLayout.set(agent.id, { top: y, height: rowH, centerY: y + rowH / 2 });
      y += rowH;
    }
    const spanCenterY = (s: Span, row: RowLayout): number => {
      const laneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(row.height / 3));
      const laneTop = row.top + 2 + (s.lane >= 0 ? s.lane : 0) * laneH;
      const laneBot = Math.min(row.top + row.height - 2, laneTop + laneH - 2);
      const rectH = Math.max(6, laneBot - laneTop);
      return laneTop + rectH / 2;
    };

    const dataLeft = GUTTER_WIDTH_PX + 10;
    const dataRight = w;
    const scratch: Span[] = [];
    // We pull a slightly wider range so edges whose endpoint sits off-screen
    // still render if the other endpoint is in view.
    const margin = this.viewport.windowMs * 0.5;
    for (const agent of agents) {
      scratch.length = 0;
      this.store.spans.queryAgent(agent.id, vs - margin, ve + margin, scratch);
      for (const s of scratch) {
        if (!s.links || s.links.length === 0) continue;
        const srcRow = rowLayout.get(s.agentId);
        if (!srcRow) continue;
        for (const link of s.links) {
          if (link.relation !== 'INVOKED') continue;
          const target = this.store.spans.get(link.targetSpanId);
          // Graceful fallback when the invoked span hasn't started yet: drop
          // the arrowhead at the target agent row's center at the source's
          // trailing edge so the user still sees the handoff intent.
          const tgtAgentId = target?.agentId ?? link.targetAgentId;
          const tgtRow = rowLayout.get(tgtAgentId);
          if (!tgtRow) continue;
          const srcY = spanCenterY(s, srcRow);
          // Source anchor: right edge of the source span (its endMs, or the
          // invoked child's start if the source is still running). This marks
          // the moment of invocation on the source's own bar.
          const srcMs = s.endMs ?? target?.startMs ?? s.startMs;
          const x1 = msToPx(this.viewport, w, srcMs);
          let x2: number;
          let tgtY: number;
          if (target) {
            x2 = msToPx(this.viewport, w, target.startMs);
            tgtY = spanCenterY(target, tgtRow);
          } else {
            x2 = x1;
            tgtY = tgtRow.centerY;
          }
          // Viewport cull: both endpoints off the same side of the data area.
          if ((x1 < dataLeft && x2 < dataLeft) || (x1 > dataRight && x2 > dataRight)) {
            continue;
          }
          const color = colorForAgent(s.agentId);
          this.edges.push({
            sourceSpanId: s.id,
            targetSpanId: target?.id ?? link.targetSpanId,
            x1,
            y1: srcY,
            x2,
            y2: tgtY,
            color,
          });
        }
      }
    }

    if (this.edges.length === 0) return;

    ctx.save();
    ctx.lineWidth = 1.25;
    ctx.globalAlpha = 0.4;
    for (const e of this.edges) {
      ctx.strokeStyle = e.color;
      ctx.beginPath();
      drawEdgePath(ctx, e.x1, e.y1, e.x2, e.y2);
      ctx.stroke();
      drawArrowhead(ctx, e.x2, e.y2, e.x1, e.y1, e.color);
    }
    ctx.restore();
  }

  // --- Context-window overlay -------------------------------------------
  //
  // Per-agent area band hugging the bottom of each row, tracing tokens/limit
  // over time. Drawn before spans (and before task chips) so anything else
  // in the row reads as foreground. The fill color is driven by the peak
  // ratio within the visible window so a row that touched critical at any
  // point reads as a red band even if the current tokens recovered, which
  // matches the user ask to surface context pressure.

  private drawContextWindowBands(ctx: CanvasRenderingContext2D): void {
    this.contextBandByAgent.clear();
    if (!this.contextOverlayVisible) return;
    const series = this.store.contextSeries;
    if (!series.hasAny()) return;

    const vs = viewportStart(this.viewport);
    const ve = this.viewport.endMs;
    const agents = this.store.agents.list;
    const w = this.widthCss;
    const leftClipPx = GUTTER_WIDTH_PX + 10;
    const rightClipPx = w;
    const msToPxBound = (ms: number): number => msToPx(this.viewport, w, ms);

    let y = TOP_MARGIN_PX;
    ctx.save();
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      if (this.hiddenAgentIds.has(agent.id)) {
        y += rowH;
        continue;
      }
      const samples = series.forAgent(agent.id);
      if (samples.length === 0) {
        y += rowH;
        continue;
      }
      const bandHeight = Math.max(10, Math.floor(rowH * 0.55));
      const geom = computeContextBandGeom({
        samples,
        viewportStartMs: vs,
        viewportEndMs: ve,
        msToPx: msToPxBound,
        leftClipPx,
        rightClipPx,
        rowTopY: y,
        rowHeight: rowH,
        bandHeight,
      });
      if (!geom) {
        y += rowH;
        continue;
      }
      const color = contextColorForRatio(geom.maxRatio);

      ctx.beginPath();
      const first = geom.top[0];
      ctx.moveTo(first.x, first.y);
      for (let i = 1; i < geom.top.length; i++) {
        ctx.lineTo(geom.top[i].x, geom.top[i].y);
      }
      ctx.lineTo(geom.top[geom.top.length - 1].x, geom.baselineY);
      ctx.lineTo(first.x, geom.baselineY);
      ctx.closePath();
      ctx.fillStyle = color.fill;
      ctx.globalAlpha = 0.18;
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(first.x, first.y);
      for (let i = 1; i < geom.top.length; i++) {
        ctx.lineTo(geom.top[i].x, geom.top[i].y);
      }
      ctx.strokeStyle = color.stroke;
      ctx.globalAlpha = 0.55;
      ctx.lineWidth = 1.5;
      ctx.stroke();

      this.contextBandByAgent.set(agent.id, {
        samples,
        bandTopYAtRatio1: geom.baselineY - bandHeight,
        baselineY: geom.baselineY,
      });

      y += rowH;
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }

  // Pointer → context sample. Used by GanttCanvas to render a DOM tooltip
  // with tokens/limit/% when the user hovers inside a row that has series
  // data. Binary searches the cached sample array; step-function semantics
  // mean "last sample whose tMs ≤ pointer time" is the value in effect.
  contextSampleAt(
    px: number,
    py: number,
  ): { agentId: string; sample: ContextWindowSample; ratio: number } | null {
    if (!this.contextOverlayVisible) return null;
    if (px < GUTTER_WIDTH_PX || py < TOP_MARGIN_PX) return null;
    const agents = this.store.agents.list;
    let y = TOP_MARGIN_PX;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      if (py >= y && py < y + rowH) {
        const cached = this.contextBandByAgent.get(agent.id);
        if (!cached || cached.samples.length === 0) return null;
        const tMs = pxToMs(this.viewport, this.widthCss, px);
        let lo = 0;
        let hi = cached.samples.length - 1;
        let idx = 0;
        while (lo <= hi) {
          const mid = (lo + hi) >>> 1;
          if (cached.samples[mid].tMs <= tMs) {
            idx = mid;
            lo = mid + 1;
          } else {
            hi = mid - 1;
          }
        }
        const sample = cached.samples[idx];
        return {
          agentId: agent.id,
          sample,
          ratio: contextRatio(sample.tokens, sample.limitTokens),
        };
      }
      y += rowH;
    }
    return null;
  }

  // --- Task plan chips + dependency edges --------------------------------

  private drawTasksAndEdges(ctx: CanvasRenderingContext2D): void {
    this.taskRectsById.clear();
    if (!this.taskPlanVisible) return;
    if (this.store.tasks.size === 0) return;
    const mode = this.taskPlanMode;
    const agents = this.store.agents.list;
    const w = this.widthCss;
    const vp = this.viewport;

    const rectsById = this.taskRectsById;

    // Pre-strip chip row lives at the top of each agent row (small pills); ghost
    // boxes live in a lower lane at predicted-time positions. Hybrid draws both.
    const stripChipH = 7;
    const stripChipW = 36;
    const stripChipGap = 3;

    let y = TOP_MARGIN_PX;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      if (this.hiddenAgentIds.has(agent.id)) {
        y += rowH;
        continue;
      }
      const tasks = this.store.tasks.tasksForAgent(agent.id);
      if (tasks.length === 0) {
        y += rowH;
        continue;
      }
      const agentColor = colorForAgent(agent.id);

      if (mode === 'pre-strip' || mode === 'hybrid') {
        // Horizontal pill rail inside the row, just under the top border.
        let px = GUTTER_WIDTH_PX + 10;
        const py = y + 2;
        for (const task of tasks) {
          if (px + stripChipW > w) break;
          this.drawTaskChip(
            ctx,
            px,
            py,
            stripChipW,
            stripChipH,
            task,
            agentColor,
            /*compact*/ true,
          );
          rectsById.set(task.id, { x: px, y: py, w: stripChipW, h: stripChipH });
          px += stripChipW + stripChipGap;
        }
      }

      if (mode === 'ghost' || mode === 'hybrid') {
        // Ghost rect at predicted time, bottom lane of the row.
        const laneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(rowH / 3));
        const ghostH = Math.max(8, laneH - 4);
        const ghostY = y + rowH - ghostH - 3;
        for (const task of tasks) {
          const startMs = task.predictedStartMs || 0;
          const durMs = Math.max(200, task.predictedDurationMs || 1000);
          const x1 = msToPx(vp, w, startMs);
          const x2 = msToPx(vp, w, startMs + durMs);
          const width = Math.max(MIN_BLOCK_WIDTH_PX, x2 - x1);
          // Viewport cull: fully off-screen.
          if (x1 + width < GUTTER_WIDTH_PX || x1 > w) continue;
          const clippedX = Math.max(GUTTER_WIDTH_PX + 10, x1);
          this.drawTaskChip(
            ctx,
            clippedX,
            ghostY,
            Math.max(MIN_BLOCK_WIDTH_PX, x1 + width - clippedX),
            ghostH,
            task,
            agentColor,
            /*compact*/ false,
          );
          // In hybrid mode, prefer the pre-strip pill as the dependency-edge
          // anchor (already set above) — it's a stable, compact rail that
          // keeps arrows readable regardless of predicted-time ordering.
          // Ghost-only mode still needs rects, so fall through when absent.
          if (!rectsById.has(task.id)) {
            rectsById.set(task.id, {
              x: clippedX,
              y: ghostY,
              w: Math.max(MIN_BLOCK_WIDTH_PX, x1 + width - clippedX),
              h: ghostH,
            });
          }
        }
      }

      y += rowH;
    }

  }

  // Draw dashed dependency curves + filled arrowheads on top of spans, using
  // the chip rects populated by drawTasksAndEdges above. Runs post-span so
  // arrows are never occluded by bar fills.
  private drawTaskDepEdges(ctx: CanvasRenderingContext2D): void {
    if (!this.taskPlanVisible) return;
    const rectsById = this.taskRectsById;
    if (rectsById.size === 0) return;
    const plans = this.store.tasks.listPlans();
    if (plans.length === 0) return;

    const depColor = cssVar('--md-sys-color-on-surface-variant') || '#9aa3b4';

    // Collect curves first; we do two passes (stroke then arrowhead) so dash
    // state doesn't leak into the filled triangles.
    type Curve = { x1: number; y1: number; cpx: number; cpy: number; x2: number; y2: number };
    const curves: Curve[] = [];
    for (const plan of plans) {
      for (const edge of plan.edges) {
        const a = rectsById.get(edge.fromTaskId);
        const b = rectsById.get(edge.toTaskId);
        if (!a || !b) continue;
        const x1 = a.x + a.w;
        const y1 = a.y + a.h / 2;
        const x2 = b.x;
        const y2 = b.y + b.h / 2;
        // Quadratic with a single control point offset perpendicular to the
        // straight line. The previous cubic used horizontal handles, which
        // collapsed onto the rail for pre-strip edges (all pills share a y)
        // and looped wildly off-canvas when the target sat left of the
        // source. A perpendicular bulge always produces a visible arc.
        const mx = (x1 + x2) / 2;
        const my = (y1 + y2) / 2;
        const dx = x2 - x1;
        const dy = y2 - y1;
        const len = Math.hypot(dx, dy) || 1;
        // Rotate the direction 90° CCW to get the perpendicular. Canvas y
        // grows downward, so a negative y component arcs "up" on screen.
        let perpX = -dy / len;
        let perpY = dx / len;
        if (perpY > 0) {
          perpX = -perpX;
          perpY = -perpY;
        }
        const bulge = Math.min(32, Math.max(12, len * 0.28));
        curves.push({
          x1,
          y1,
          cpx: mx + perpX * bulge,
          cpy: my + perpY * bulge,
          x2,
          y2,
        });
      }
    }
    if (curves.length === 0) return;

    ctx.save();
    ctx.globalAlpha = 0.5;
    ctx.setLineDash([4, 3]);
    ctx.lineWidth = 1;
    ctx.strokeStyle = depColor;
    for (const c of curves) {
      ctx.beginPath();
      ctx.moveTo(c.x1, c.y1);
      ctx.quadraticCurveTo(c.cpx, c.cpy, c.x2, c.y2);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.globalAlpha = 0.85;
    ctx.fillStyle = depColor;
    for (const c of curves) {
      // Tangent of a quadratic bezier at t=1 is parallel to (P2 - P1), i.e.
      // (tip - cp). Fall back to the straight source→target vector if the
      // control coincides with the endpoint (degenerate zero-length edge).
      let tx = c.x2 - c.cpx;
      let ty = c.y2 - c.cpy;
      let len = Math.hypot(tx, ty);
      if (len < 0.01) {
        tx = c.x2 - c.x1;
        ty = c.y2 - c.y1;
        len = Math.hypot(tx, ty) || 1;
      }
      const ux = tx / len;
      const uy = ty / len;
      const size = 8;
      const base = 7;
      const bx = c.x2 - ux * size;
      const by = c.y2 - uy * size;
      const nx = -uy;
      const ny = ux;
      ctx.beginPath();
      ctx.moveTo(c.x2, c.y2);
      ctx.lineTo(bx + nx * (base / 2), by + ny * (base / 2));
      ctx.lineTo(bx - nx * (base / 2), by - ny * (base / 2));
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  private drawTaskChip(
    ctx: CanvasRenderingContext2D,
    x: number,
    y: number,
    w: number,
    h: number,
    task: Task,
    agentColor: string,
    compact: boolean,
  ): void {
    const status: TaskStatus = task.status;
    const isPending = status === 'PENDING' || status === 'UNSPECIFIED';
    const isRunning = status === 'RUNNING';
    const isDone = status === 'COMPLETED';
    const isFailed = status === 'FAILED';
    const isCancelled = status === 'CANCELLED';

    ctx.save();
    if (isFailed) {
      // Red dashed outline, transparent fill.
      ctx.strokeStyle = cssVar('--md-sys-color-error') || '#ff6b6b';
      ctx.setLineDash([3, 2]);
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.85;
      ctx.strokeRect(x + 0.5, y + 0.5, Math.max(1, w - 1), Math.max(1, h - 1));
      ctx.setLineDash([]);
    } else {
      // Dim outline + faint fill, color from agent.
      ctx.fillStyle = agentColor;
      ctx.globalAlpha = isRunning ? 0.55 : isDone ? 0.4 : isCancelled ? 0.15 : 0.2;
      ctx.fillRect(x, y, w, h);
      ctx.globalAlpha = isPending ? 0.45 : 0.8;
      ctx.strokeStyle = agentColor;
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 0.5, y + 0.5, Math.max(1, w - 1), Math.max(1, h - 1));
    }
    ctx.globalAlpha = 1;

    if (isDone && w >= 10) {
      // Checkmark at right edge.
      ctx.fillStyle = cssVar('--md-sys-color-on-surface') || '#e2e2e9';
      ctx.font = `${Math.max(8, Math.min(11, h))}px system-ui, sans-serif`;
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'right';
      ctx.fillText('✓', x + w - 2, y + h / 2);
      ctx.textAlign = 'start';
    }

    if (!compact && w > 28 && !isDone) {
      // Task title label — kept short; clip to the chip.
      ctx.save();
      ctx.beginPath();
      ctx.rect(x, y, w, h);
      ctx.clip();
      ctx.fillStyle = cssVar('--md-sys-color-on-surface-variant') || '#c3c6cf';
      ctx.font = '10px system-ui, sans-serif';
      ctx.textBaseline = 'middle';
      const label = task.title || '(task)';
      ctx.fillText(label, x + 4, y + h / 2);
      if (isCancelled) {
        ctx.strokeStyle = cssVar('--md-sys-color-on-surface-variant') || '#c3c6cf';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x + 2, y + h / 2);
        ctx.lineTo(x + w - 2, y + h / 2);
        ctx.stroke();
      }
      ctx.restore();
    }
    ctx.restore();
  }

  private edgeHitTest(px: number, py: number): number | null {
    const edges = this.edges;
    if (edges.length === 0) return null;
    const tol = 5;
    const samples = 20;
    for (let i = edges.length - 1; i >= 0; i--) {
      const e = edges[i];
      // Quick bbox reject with padding for curve bulge.
      const minX = Math.min(e.x1, e.x2) - tol;
      const maxX = Math.max(e.x1, e.x2) + tol;
      const minY = Math.min(e.y1, e.y2) - 40;
      const maxY = Math.max(e.y1, e.y2) + 40;
      if (px < minX || px > maxX || py < minY || py > maxY) continue;
      const [cp1x, cp1y, cp2x, cp2y] = edgeControlPoints(e.x1, e.y1, e.x2, e.y2);
      let prevX = e.x1;
      let prevY = e.y1;
      for (let j = 1; j <= samples; j++) {
        const t = j / samples;
        const mt = 1 - t;
        const x =
          mt * mt * mt * e.x1 +
          3 * mt * mt * t * cp1x +
          3 * mt * t * t * cp2x +
          t * t * t * e.x2;
        const y =
          mt * mt * mt * e.y1 +
          3 * mt * mt * t * cp1y +
          3 * mt * t * t * cp2y +
          t * t * t * e.y2;
        if (pointSegmentDist(px, py, prevX, prevY, x, y) <= tol) return i;
        prevX = x;
        prevY = y;
      }
    }
    return null;
  }

  lastDrawnCount = 0;
  // Exposed for task #4 tests: how many brain badges were drawn on the
  // most recent drawBlocks pass. Lets tests assert the LLM_CALL → badge
  // mapping without having to diff pixels.
  lastBrainBadgeCount = 0;

  // --- Overlay -----------------------------------------------------------

  private drawOverlay(): void {
    const ctx = this.overlayCtx;
    if (!ctx) return;
    const w = this.widthCss;
    const h = this.heightCss;
    ctx.clearRect(0, 0, w, h);

    // Now cursor
    if (!this.viewport.replay) {
      const nowX = msToPx(this.viewport, w, this.store.nowMs);
      if (nowX >= GUTTER_WIDTH_PX && nowX <= w) {
        ctx.strokeStyle = cssVar('--md-sys-color-primary') || '#a8c8ff';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(nowX, TOP_MARGIN_PX);
        ctx.lineTo(nowX, h);
        ctx.stroke();
      }
    }

    // Breathing/pulse animations for RUNNING and AWAITING_HUMAN spans in view.
    this.drawAnimatedSpans(ctx);

    // Hover highlight
    if (this.hoveredSpanId) {
      const rect = this.rectForSpan(this.hoveredSpanId);
      if (rect) {
        ctx.strokeStyle = cssVar('--md-sys-color-on-surface') || '#e2e2e9';
        ctx.lineWidth = 2;
        ctx.strokeRect(rect.x - 1, rect.y - 1, rect.w + 2, rect.h + 2);
      }
    }

    // Edge hover highlight: brighten the curve + both endpoint rectangles so
    // the cross-agent link reads clearly.
    if (this.hoveredEdgeIdx !== null) {
      const e = this.edges[this.hoveredEdgeIdx];
      if (e) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(GUTTER_WIDTH_PX, TOP_MARGIN_PX, w - GUTTER_WIDTH_PX, h - TOP_MARGIN_PX);
        ctx.clip();
        ctx.strokeStyle = e.color;
        ctx.globalAlpha = 0.95;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        drawEdgePath(ctx, e.x1, e.y1, e.x2, e.y2);
        ctx.stroke();
        drawArrowhead(ctx, e.x2, e.y2, e.x1, e.y1, e.color);
        ctx.restore();
        const srcRect = this.rectForSpan(e.sourceSpanId);
        const tgtRect = this.rectForSpan(e.targetSpanId);
        ctx.save();
        ctx.strokeStyle = e.color;
        ctx.lineWidth = 2;
        ctx.globalAlpha = 0.9;
        if (srcRect) ctx.strokeRect(srcRect.x - 1, srcRect.y - 1, srcRect.w + 2, srcRect.h + 2);
        if (tgtRect) ctx.strokeRect(tgtRect.x - 1, tgtRect.y - 1, tgtRect.w + 2, tgtRect.h + 2);
        ctx.restore();
      }
    }

    // Selection highlight: draw the task-driven halo first so the canvas-click
    // selection stroke (below) sits on top. Using whichever span id resolves
    // first lets a task selection light up the matching bar even when nothing
    // was directly clicked in the canvas.
    const haloSpanId = this.selectedTaskSpanId ?? this.selectedSpanId;
    if (haloSpanId) {
      const rect = this.rectForSpan(haloSpanId);
      if (rect) {
        const primary = cssVar('--md-sys-color-primary') || '#a8c8ff';
        ctx.save();
        // Outer glow — canvas shadow blurred outward.
        ctx.shadowColor = primary;
        ctx.shadowBlur = 14;
        ctx.strokeStyle = primary;
        ctx.lineWidth = 3;
        ctx.strokeRect(rect.x - 2, rect.y - 2, rect.w + 4, rect.h + 4);
        ctx.restore();
      }
    }
    if (this.selectedSpanId && this.selectedSpanId !== this.selectedTaskSpanId) {
      const rect = this.rectForSpan(this.selectedSpanId);
      if (rect) {
        ctx.strokeStyle = cssVar('--md-sys-color-primary') || '#a8c8ff';
        ctx.lineWidth = 3;
        ctx.strokeRect(rect.x - 2, rect.y - 2, rect.w + 4, rect.h + 4);
      }
    }
  }

  private drawAnimatedSpans(ctx: CanvasRenderingContext2D): void {
    const vs = viewportStart(this.viewport);
    const ve = this.viewport.endMs;
    const agents = this.store.agents.list;
    const t = performance.now() / 1000;
    // Two phase oscillators.
    const breathe = 0.85 + 0.15 * (0.5 + 0.5 * Math.sin(t * Math.PI));
    const pulse = 0.5 + 0.5 * Math.sin(t * 2 * Math.PI);
    let y = TOP_MARGIN_PX;
    const scratch: Span[] = [];
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      scratch.length = 0;
      this.store.spans.queryAgent(agent.id, vs, ve, scratch);
      for (const s of scratch) {
        if (s.status !== 'RUNNING' && s.status !== 'AWAITING_HUMAN') continue;
        const x1 = Math.max(GUTTER_WIDTH_PX + 10, msToPx(this.viewport, this.widthCss, s.startMs));
        const x2 = msToPx(this.viewport, this.widthCss, s.endMs ?? this.store.nowMs);
        const width = Math.max(MIN_BLOCK_WIDTH_PX, x2 - x1);
        const laneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(rowH / 3));
        const laneTop = y + 2 + (s.lane >= 0 ? s.lane : 0) * laneH;
        const rectH = Math.max(6, Math.min(y + rowH - 2, laneTop + laneH - 2) - laneTop);
        if (s.status === 'RUNNING') {
          ctx.globalAlpha = breathe * 0.35;
          ctx.fillStyle = cssVar('--md-sys-color-primary') || '#a8c8ff';
          ctx.fillRect(x1, laneTop, width, rectH);
        } else {
          ctx.globalAlpha = 0.4 + pulse * 0.5;
          ctx.fillStyle = cssVar('--md-sys-color-error') || '#ffb4ab';
          ctx.fillRect(x1 - 2, laneTop - 2, width + 4, rectH + 4);
        }
      }
      y += rowH;
    }
    ctx.globalAlpha = 1;
  }

  // --- Hit testing -------------------------------------------------------

  private hitTest(px: number, py: number): string | null {
    if (px < GUTTER_WIDTH_PX || py < TOP_MARGIN_PX) return null;
    const agents = this.store.agents.list;
    let y = TOP_MARGIN_PX;
    const scratch: Span[] = [];
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      if (this.hiddenAgentIds.has(agent.id)) {
        y += rowH;
        continue;
      }
      if (py >= y && py < y + rowH) {
        const vs = viewportStart(this.viewport);
        const ve = this.viewport.endMs;
        scratch.length = 0;
        this.store.spans.queryAgent(agent.id, vs, ve, scratch);
        // Iterate back-to-front so topmost sublane wins.
        for (let i = scratch.length - 1; i >= 0; i--) {
          const s = scratch[i];
          const x1 = Math.max(GUTTER_WIDTH_PX + 10, msToPx(this.viewport, this.widthCss, s.startMs));
          const x2 = msToPx(this.viewport, this.widthCss, s.endMs ?? this.store.nowMs);
          const width = Math.max(MIN_BLOCK_WIDTH_PX, x2 - x1);
          const laneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(rowH / 3));
          const laneTop = y + 2 + (s.lane >= 0 ? s.lane : 0) * laneH;
          const rectH = Math.max(6, Math.min(y + rowH - 2, laneTop + laneH - 2) - laneTop);
          // Expand hit zone by 4px on each side so small blocks are easier to click.
          if (px >= x1 - 4 && px <= x1 + width + 4 && py >= laneTop && py <= laneTop + rectH) {
            return s.id;
          }
        }
        return null;
      }
      y += rowH;
    }
    return null;
  }

  private rectForSpan(spanId: string): { x: number; y: number; w: number; h: number } | null {
    const s = this.store.spans.get(spanId);
    if (!s) return null;
    const agents = this.store.agents.list;
    let y = TOP_MARGIN_PX;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
      if (agent.id === s.agentId) {
        const x1 = Math.max(
          GUTTER_WIDTH_PX + 10,
          msToPx(this.viewport, this.widthCss, s.startMs),
        );
        const x2 = msToPx(this.viewport, this.widthCss, s.endMs ?? this.store.nowMs);
        const width = Math.max(MIN_BLOCK_WIDTH_PX, x2 - x1);
        const laneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(rowH / 3));
        const laneTop = y + 2 + (s.lane >= 0 ? s.lane : 0) * laneH;
        const rectH = Math.max(6, Math.min(y + rowH - 2, laneTop + laneH - 2) - laneTop);
        return { x: x1, y: laneTop, w: width, h: rectH };
      }
      y += rowH;
    }
    return null;
  }

  private rowHeight(agentId: string): number {
    if (this.hiddenAgentIds.has(agentId)) return ROW_HEIGHT_COLLAPSED_PX;
    return agentId === this.focusedAgentId ? ROW_HEIGHT_FOCUSED_PX : ROW_HEIGHT_PX;
  }

  // --- Perf ---------------------------------------------------------------

  p95FrameMs(): number {
    const n = this.metrics.sampleCount;
    if (n === 0) return 0;
    const arr = Array.from(this.metrics.samples.subarray(0, n)).sort((a, b) => a - b);
    return arr[Math.floor(n * 0.95)] ?? arr[n - 1];
  }

  resetMetrics(): void {
    this.metrics.sampleCount = 0;
    this.metrics.sampleIdx = 0;
  }
}

// --- helpers ------------------------------------------------------------

function edgeControlPoints(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): [number, number, number, number] {
  // Horizontal tangents at both ends; offset scales with dx so short hops
  // curve gently and long jumps arc broadly. Falls back to a fixed minimum
  // when dx is near zero so near-vertical links still visibly curve.
  const dx = Math.max(40, Math.abs(x2 - x1) * 0.4);
  return [x1 + dx, y1, x2 - dx, y2];
}

function drawEdgePath(
  ctx: CanvasRenderingContext2D,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): void {
  const [cp1x, cp1y, cp2x, cp2y] = edgeControlPoints(x1, y1, x2, y2);
  ctx.moveTo(x1, y1);
  ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, x2, y2);
}

function drawArrowhead(
  ctx: CanvasRenderingContext2D,
  tipX: number,
  tipY: number,
  fromX: number,
  fromY: number,
  color: string,
): void {
  // The incoming bezier arrives close to horizontal at the target (control
  // point cp2 shares y2), so approximating direction with (tip - cp2) reads
  // better than using the far source point directly.
  const [, , cp2x, cp2y] = edgeControlPoints(fromX, fromY, tipX, tipY);
  const dx = tipX - cp2x;
  const dy = tipY - cp2y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const size = 7;
  const base = 6;
  const ax = tipX - ux * size;
  const ay = tipY - uy * size;
  const px = -uy;
  const py = ux;
  ctx.save();
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(tipX, tipY);
  ctx.lineTo(ax + px * (base / 2), ay + py * (base / 2));
  ctx.lineTo(ax - px * (base / 2), ay - py * (base / 2));
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

function pointSegmentDist(
  px: number,
  py: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (len2 === 0) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function roundRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  ctx.lineTo(x + rr, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - rr);
  ctx.lineTo(x, y + rr);
  ctx.quadraticCurveTo(x, y, x + rr, y);
}

function truncate(ctx: CanvasRenderingContext2D, text: string, maxW: number): string {
  if (ctx.measureText(text).width <= maxW) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    const s = text.slice(0, mid) + '…';
    if (ctx.measureText(s).width <= maxW) lo = mid + 1;
    else hi = mid;
  }
  return text.slice(0, Math.max(0, lo - 1)) + '…';
}

function pickTickMs(windowMs: number): number {
  // Target ~8 ticks across the viewport.
  const targets = [
    1_000,       // 1s
    5_000,       // 5s
    15_000,      // 15s
    60_000,      // 1min
    5 * 60_000,  // 5min
    15 * 60_000, // 15min
  ];
  for (const t of targets) {
    if (windowMs / t < 12) return t;
  }
  return 15 * 60_000;
}

function formatTickLabel(ms: number, tickMs: number): string {
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (tickMs >= 60_000) {
    return h > 0 ? `${h}:${String(m).padStart(2, '0')}` : `${m}m`;
  }
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}
