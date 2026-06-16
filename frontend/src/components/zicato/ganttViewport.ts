// ganttViewport.ts — the FROZEN viewport math shared by GanttZ + MinimapZ +
// GanttViewZ for the zoom/minimap feature. A GanttView is the visible time
// window in seconds; all four helpers keep it inside [0, T] with a sane minimum
// width, so a zoomed/panned gantt never escapes the session range.
//
// Pure, no React, no DOM. The renderers map this window onto plot pixels.

/** Visible window in seconds, 0 <= t0 < t1 <= T. */
export type GanttView = { t0: number; t1: number };

/** The minimum visible window (seconds): never zoom past T/200 (floor 1s). */
function minWindow(T: number): number {
  return Math.max(1, T / 200);
}

/** The full session range as a view. */
export function fitView(T: number): GanttView {
  return { t0: 0, t1: T };
}

/**
 * Zoom by `factor` (factor < 1 = zoom IN, > 1 = zoom OUT) keeping `focusT` at the
 * same SCREEN FRACTION it currently occupies, then clamp the window into [0, T]
 * with a minimum width of max(1, T/200).
 */
export function zoomView(
  v: GanttView,
  factor: number,
  focusT: number,
  T: number,
): GanttView {
  const oldW = v.t1 - v.t0;
  if (oldW <= 0) return fitView(T);
  // The screen fraction of focusT within the current window (clamped 0..1 so a
  // focus outside the window still produces a sane anchor).
  const frac = Math.min(1, Math.max(0, (focusT - v.t0) / oldW));
  const minW = minWindow(T);
  // New width: clamp to [minW, T].
  const newW = Math.min(T, Math.max(minW, oldW * factor));
  // Keep focusT at the same fraction: focusT = t0 + frac*newW.
  let t0 = focusT - frac * newW;
  let t1 = t0 + newW;
  // Slide the window back inside [0, T] without changing its width.
  if (t0 < 0) {
    t0 = 0;
    t1 = newW;
  }
  if (t1 > T) {
    t1 = T;
    t0 = T - newW;
  }
  if (t0 < 0) t0 = 0;
  return { t0, t1 };
}

/**
 * Pan by `dt` seconds (positive → window moves later), keeping the window WIDTH
 * fixed and clamping into [0, T].
 */
export function panView(v: GanttView, dt: number, T: number): GanttView {
  const w = Math.min(T, v.t1 - v.t0);
  let t0 = v.t0 + dt;
  let t1 = t0 + w;
  if (t0 < 0) {
    t0 = 0;
    t1 = w;
  }
  if (t1 > T) {
    t1 = T;
    t0 = T - w;
  }
  if (t0 < 0) t0 = 0;
  return { t0, t1 };
}

/** True when the view is (within epsilon) the full [0, T] range. */
export function isFit(v: GanttView, T: number): boolean {
  const eps = Math.max(1e-6, T * 1e-4);
  return Math.abs(v.t0) <= eps && Math.abs(v.t1 - T) <= eps;
}
