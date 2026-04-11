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
import { bucketKey, cssVar, refreshThemeCache, resolveStyle } from './colors';
import type { SessionStore } from './index';
import type { SpanIndex, DirtyRect } from './spatialIndex';
import type { Agent, Span, SpanKind } from './types';
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
  onSelect?: (spanId: string | null) => void;
  onHoverChange?: (hover: HoverState | null) => void;
  onViewportChange?: (v: ViewportState) => void;
}

export interface HoverState {
  spanId: string;
  // Tooltip anchor in CSS pixels, relative to the canvas container.
  x: number;
  y: number;
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
  private selectedSpanId: string | null = null;
  private hoveredSpanId: string | null = null;

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
    const hit = this.hitTest(x, y);
    this.selectedSpanId = hit;
    this.overlayDirty = true;
    this.cb.onSelect?.(hit);
  }

  handlePointerMove(x: number, y: number): void {
    const hit = this.hitTest(x, y);
    if (hit !== this.hoveredSpanId) {
      this.hoveredSpanId = hit;
      this.overlayDirty = true;
      this.cb.onHoverChange?.(hit ? { spanId: hit, x, y } : null);
    }
  }

  handlePointerLeave(): void {
    if (this.hoveredSpanId) {
      this.hoveredSpanId = null;
      this.overlayDirty = true;
      this.cb.onHoverChange?.(null);
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

  fitAll(): void {
    const maxEnd = Math.max(this.store.spans.maxEndMs(), this.store.nowMs, 1);
    const window = Math.min(ZOOM_MAX_MS, Math.max(ZOOM_MIN_MS, maxEnd * 1.05));
    this.setViewport({
      ...this.viewport,
      endMs: maxEnd,
      windowMs: window,
      liveFollow: false,
    });
  }

  focusAgent(agentId: string | null): void {
    this.focusedAgentId = agentId;
    this.bgDirty = true;
    this.blocksDirty = true;
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

  private frame(): void {
    // Live follow advance
    this.viewport = advanceLive(this.viewport, this.store.nowMs);

    // Any in-viewport animated state forces overlay redraw every frame.
    const hasAnimatedInView = this.hasAnimatedInViewport();
    if (hasAnimatedInView) this.overlayDirty = true;

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
    // Color chip
    const chipX = 12;
    const chipY = y + rowH / 2 - 6;
    ctx.fillStyle = colorForAgent(agent.id);
    roundRectPath(ctx, chipX, chipY, 12, 12, 3);
    ctx.fill();

    // Name
    ctx.fillStyle = cssVar('--md-sys-color-on-surface') || '#e2e2e9';
    ctx.font = '600 13px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    const name = agent.name || agent.id;
    const maxNameW = GUTTER_WIDTH_PX - 80;
    ctx.fillText(truncate(ctx, name, maxNameW), chipX + 18, y + rowH / 2 - 7);

    // Framework badge
    ctx.font = '10px system-ui, sans-serif';
    ctx.fillStyle = cssVar('--md-sys-color-on-surface-variant') || '#c3c6cf';
    ctx.fillText(agent.framework, chipX + 18, y + rowH / 2 + 8);

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

    let y = TOP_MARGIN_PX;
    let visibleCount = 0;
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
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
        if (width >= 24) {
          b.labels.push({ s, x: x1, y: laneTop, w: width, h: rectH });
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

    // Labels pass (text is expensive — only for blocks >= 24px wide).
    ctx.font = '11px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = cssVar('--md-sys-color-on-surface') || '#e2e2e9';
    for (const b of buckets.values()) {
      for (const l of b.labels) {
        const { s, x, y: ly, w: lw, h: lh } = l;
        const icon = KIND_ICON[s.kind];
        const label = `${icon} ${s.name}`;
        ctx.save();
        ctx.beginPath();
        ctx.rect(x + 4, ly, lw - 8, lh);
        ctx.clip();
        ctx.fillText(label, x + 6, ly + lh / 2);
        ctx.restore();
      }
    }

    ctx.restore();
    // Expose last-draw count for stress tooling.
    this.lastDrawnCount = visibleCount;
  }

  lastDrawnCount = 0;

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

    // Selection highlight
    if (this.selectedSpanId) {
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

  private hasAnimatedInViewport(): boolean {
    const vs = viewportStart(this.viewport);
    const ve = this.viewport.endMs;
    const scratch: Span[] = [];
    for (const agent of this.store.agents.list) {
      scratch.length = 0;
      this.store.spans.queryAgent(agent.id, vs, ve, scratch);
      for (const s of scratch) {
        if (s.status === 'RUNNING' || s.status === 'AWAITING_HUMAN') return true;
      }
    }
    return false;
  }

  // --- Hit testing -------------------------------------------------------

  private hitTest(px: number, py: number): string | null {
    if (px < GUTTER_WIDTH_PX || py < TOP_MARGIN_PX) return null;
    const agents = this.store.agents.list;
    let y = TOP_MARGIN_PX;
    const scratch: Span[] = [];
    for (const agent of agents) {
      const rowH = this.rowHeight(agent.id);
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
          if (px >= x1 && px <= x1 + width && py >= laneTop && py <= laneTop + rectH) {
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
