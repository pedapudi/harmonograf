// Pure viewport math for the sequence diagram (GraphView) zoom/pan/minimap.
//
// Coordinate systems:
//   - "content" coords are the raw SVG coordinates produced by the sequence
//     layout (what the existing <svg width=svgW height=svgH> renders into).
//   - "container" coords are CSS pixels inside the scroll viewport div that
//     hosts the SVG. The container is fixed-size; we apply a single affine
//     transform to the inner <g> so that:
//
//         container = content * scale + (tx, ty)
//
// The functions below are pure so they can be unit-tested without a DOM.

export interface Viewport {
  scale: number;
  tx: number;
  ty: number;
}

export interface Size {
  w: number;
  h: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export const MIN_SCALE = 0.25;
export const MAX_SCALE = 4;

export const DEFAULT_VIEWPORT: Viewport = { scale: 1, tx: 0, ty: 0 };

export function clampScale(s: number): number {
  if (Number.isNaN(s)) return 1;
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, s));
}

// Apply a zoom factor while keeping the point (cx, cy) — given in container
// pixels — anchored under the cursor. This is the "wheel zoom centered on
// cursor" behavior: the content pixel that was under the mouse before the
// zoom is still under the mouse after.
export function zoomAt(
  vp: Viewport,
  factor: number,
  cx: number,
  cy: number,
): Viewport {
  const next = clampScale(vp.scale * factor);
  if (next === vp.scale) return vp;
  const k = next / vp.scale;
  return {
    scale: next,
    tx: cx - (cx - vp.tx) * k,
    ty: cy - (cy - vp.ty) * k,
  };
}

export function panBy(vp: Viewport, dx: number, dy: number): Viewport {
  return { scale: vp.scale, tx: vp.tx + dx, ty: vp.ty + dy };
}

// Fit a content-space rectangle into a container, leaving `padding` CSS px of
// breathing room around it, and clamp the resulting scale into the allowed
// range. The content is centered inside the container.
//
// ``maxScale`` overrides the default upper clamp so callers (e.g. the
// "fit selection" affordance) can keep the zoom from blowing up on a
// tiny selection, and the initial-fit path can cap the lower bound at
// 1.0 so the overview opens at 100% (the previous behaviour fit big DAGs
// at 37% which left the canvas mostly empty — see Item 2 of the UX
// cleanup batch). Pass ``minScale`` to clamp upward (≥ minScale) so a
// large content rect doesn't render at 0.3× and look like the panel's
// broken.
export function fitRect(
  content: Rect,
  container: Size,
  padding = 24,
  opts: { minScale?: number; maxScale?: number } = {},
): Viewport {
  const availW = Math.max(1, container.w - padding * 2);
  const availH = Math.max(1, container.h - padding * 2);
  const bw = Math.max(1, content.w);
  const bh = Math.max(1, content.h);
  let scale = clampScale(Math.min(availW / bw, availH / bh));
  if (opts.maxScale !== undefined) scale = Math.min(scale, opts.maxScale);
  if (opts.minScale !== undefined) scale = Math.max(scale, opts.minScale);
  const tx = padding + (availW - bw * scale) / 2 - content.x * scale;
  const ty = padding + (availH - bh * scale) / 2 - content.y * scale;
  return { scale, tx, ty };
}

// Inverse of the affine: container point → content point.
export function containerToContent(
  vp: Viewport,
  cx: number,
  cy: number,
): { x: number; y: number } {
  return { x: (cx - vp.tx) / vp.scale, y: (cy - vp.ty) / vp.scale };
}

// The slice of content that is currently visible inside the container.
export function visibleContentRect(vp: Viewport, container: Size): Rect {
  return {
    x: -vp.tx / vp.scale,
    y: -vp.ty / vp.scale,
    w: container.w / vp.scale,
    h: container.h / vp.scale,
  };
}

// Center the viewport on a given content point without changing scale.
export function centerOn(
  vp: Viewport,
  contentX: number,
  contentY: number,
  container: Size,
): Viewport {
  return {
    scale: vp.scale,
    tx: container.w / 2 - contentX * vp.scale,
    ty: container.h / 2 - contentY * vp.scale,
  };
}

// Project a content-space rectangle (e.g., the current visible area) onto the
// minimap's own pixel grid. The minimap renders the full `contentBounds` into
// a fixed `minimap` size with uniform x/y scales per axis.
export function minimapViewportRect(
  visible: Rect,
  contentBounds: Rect,
  minimap: Size,
): Rect {
  const sx = minimap.w / Math.max(1, contentBounds.w);
  const sy = minimap.h / Math.max(1, contentBounds.h);
  return {
    x: (visible.x - contentBounds.x) * sx,
    y: (visible.y - contentBounds.y) * sy,
    w: visible.w * sx,
    h: visible.h * sy,
  };
}

// Inverse: a point clicked on the minimap (in minimap px) maps back to content
// coordinates so we can center the main viewport there.
export function minimapPointToContent(
  mx: number,
  my: number,
  contentBounds: Rect,
  minimap: Size,
): { x: number; y: number } {
  const sx = contentBounds.w / Math.max(1, minimap.w);
  const sy = contentBounds.h / Math.max(1, minimap.h);
  return { x: contentBounds.x + mx * sx, y: contentBounds.y + my * sy };
}

// Step-zoom used by the +/-/0 keyboard shortcuts. Applied as a multiplicative
// factor so repeated presses keep a constant ratio.
export const ZOOM_STEP = 1.25;

export function zoomStep(
  vp: Viewport,
  direction: 'in' | 'out' | 'reset',
  container: Size,
): Viewport {
  if (direction === 'reset') {
    return { scale: 1, tx: 0, ty: 0 };
  }
  const factor = direction === 'in' ? ZOOM_STEP : 1 / ZOOM_STEP;
  return zoomAt(vp, factor, container.w / 2, container.h / 2);
}

// Wheel deltas come in varying units (pixel, line, page) and signs. Convert a
// deltaY into a multiplicative zoom factor. Positive delta → zoom out.
export function wheelZoomFactor(deltaY: number): number {
  const clamped = Math.max(-100, Math.min(100, deltaY));
  return Math.exp(-clamped * 0.0015);
}
