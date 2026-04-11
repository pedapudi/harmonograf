// Pan/zoom state for the Gantt. Zoom is expressed as "visible window duration
// in milliseconds" (matches the unit used elsewhere). Pan is expressed as "end
// of visible window in ms since session start" — we anchor to the right edge
// so live sessions naturally follow time without a separate follow offset.

export const ZOOM_MIN_MS = 30_000;       // 30s
export const ZOOM_MAX_MS = 6 * 3600_000; // 6h
export const GUTTER_WIDTH_PX = 200;
export const ROW_HEIGHT_PX = 56;
export const ROW_HEIGHT_FOCUSED_PX = 120;
export const SUB_LANE_HEIGHT_PX = 14;
export const TOP_MARGIN_PX = 24; // room for time axis

export interface ViewportState {
  // Session-relative ms at the right edge of the viewport.
  endMs: number;
  // Visible window duration.
  windowMs: number;
  // Whether the viewport follows the now cursor (set to false on user pan/zoom).
  liveFollow: boolean;
  // True in replay mode — live cursor is hidden and pan has no right limit.
  replay: boolean;
}

export function defaultViewport(): ViewportState {
  const windowMs = 5 * 60 * 1000;
  return {
    // Anchor so viewportStart === 0: a fresh session shows from t=0, not -5m.
    endMs: windowMs,
    windowMs,
    liveFollow: true,
    replay: false,
  };
}

export function viewportStart(v: ViewportState): number {
  return v.endMs - v.windowMs;
}

// Clamp so the left edge never sits before session start (session-relative 0).
// Preserves window size by pushing endMs forward if needed.
function clampEnd(v: ViewportState, endMs: number): number {
  return Math.max(v.windowMs, endMs);
}

export function msToPx(v: ViewportState, widthPx: number, ms: number): number {
  const widthAvailable = widthPx - GUTTER_WIDTH_PX;
  return GUTTER_WIDTH_PX + ((ms - viewportStart(v)) / v.windowMs) * widthAvailable;
}

export function pxToMs(v: ViewportState, widthPx: number, px: number): number {
  const widthAvailable = widthPx - GUTTER_WIDTH_PX;
  return viewportStart(v) + ((px - GUTTER_WIDTH_PX) / widthAvailable) * v.windowMs;
}

// Pan by a fraction of the visible window (e.g. 0.1 = 10% right). Disables
// live follow. Left edge is clamped to session start.
export function pan(v: ViewportState, fraction: number): ViewportState {
  const nextEnd = v.endMs + v.windowMs * fraction;
  return { ...v, endMs: clampEnd(v, nextEnd), liveFollow: false };
}

// Zoom around a focal point expressed in ms (session-relative). Positive delta
// = zoom in. Clamped to [ZOOM_MIN_MS, ZOOM_MAX_MS].
export function zoomAround(
  v: ViewportState,
  focusMs: number,
  factor: number,
): ViewportState {
  const newWindow = Math.max(ZOOM_MIN_MS, Math.min(ZOOM_MAX_MS, v.windowMs / factor));
  // Preserve focusMs's fractional position within the viewport.
  const frac = (focusMs - viewportStart(v)) / v.windowMs;
  const newStart = focusMs - frac * newWindow;
  const newEnd = Math.max(newWindow, newStart + newWindow);
  return { ...v, windowMs: newWindow, endMs: newEnd, liveFollow: false };
}

// Advance the right edge to track `nowMs` if liveFollow is enabled. The cursor
// sits at 80% of the visible width, so the anchor is nowMs + 20% of window.
export function advanceLive(v: ViewportState, nowMs: number): ViewportState {
  if (!v.liveFollow || v.replay) return v;
  // Left edge never goes before session start (t=0). For a session younger
  // than windowMs, pin the viewport to [0, windowMs] so the live cursor slides
  // in from the left rather than appearing mid-canvas against empty past.
  const targetEnd = clampEnd(v, nowMs + v.windowMs * 0.2);
  if (targetEnd === v.endMs) return v;
  return { ...v, endMs: targetEnd };
}

export function returnToLive(v: ViewportState, nowMs: number): ViewportState {
  return advanceLive({ ...v, liveFollow: true }, nowMs);
}
