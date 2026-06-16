// ganttViewport.ts — the viewport math shared by GanttZ + MinimapZ + GanttViewZ
// for the zoom/minimap feature. A GanttView is the visible time window in
// seconds; a GanttDomain is the OUTER bound the window is kept inside.
//
// The domain is the CONTENT range — [first span start, last span end] — not
// [0, sessionEnd]. Sessions often have a lead-in (agents take ~30s+ to connect
// and emit their first span); bounding the viewport to the content means "fit"
// snaps to where the spans actually are instead of showing an empty band, and
// pan/zoom never wander into dead time.
//
// Pure, no React, no DOM. The renderers map this window onto plot pixels.

/** Visible window in seconds, dom.lo <= t0 < t1 <= dom.hi. */
export type GanttView = { t0: number; t1: number };

/** Outer time bound the view is kept inside (the content range). */
export type GanttDomain = { lo: number; hi: number };

/** The minimum visible window (seconds): never zoom past span/200 (floor 1s). */
function minWindow(d: GanttDomain): number {
  return Math.max(1, (d.hi - d.lo) / 200);
}

/** The full content range as a view. */
export function fitView(d: GanttDomain): GanttView {
  return { t0: d.lo, t1: d.hi };
}

/**
 * Derive the content domain from a session's spans: [min t0, max t1]. Falls
 * back to [0, fallbackHi] (or [0, 30]) when there are no spans / a degenerate
 * range, so an empty session still renders a frame.
 */
export function contentDomain(
  spans: ReadonlyArray<{ t0: number; t1: number }>,
  fallbackHi: number,
): GanttDomain {
  let lo = Number.POSITIVE_INFINITY;
  let hi = Number.NEGATIVE_INFINITY;
  for (const s of spans) {
    if (s.t0 < lo) lo = s.t0;
    if (s.t1 > hi) hi = s.t1;
  }
  if (!Number.isFinite(lo) || hi <= lo) {
    return { lo: 0, hi: fallbackHi > 0 ? fallbackHi : 30 };
  }
  return { lo, hi };
}

/**
 * Zoom by `factor` (factor < 1 = zoom IN, > 1 = zoom OUT) keeping `focusT` at the
 * same SCREEN FRACTION it currently occupies, then clamp the window into the
 * domain with a minimum width of max(1, span/200).
 */
export function zoomView(
  v: GanttView,
  factor: number,
  focusT: number,
  d: GanttDomain,
): GanttView {
  const oldW = v.t1 - v.t0;
  if (oldW <= 0) return fitView(d);
  // The screen fraction of focusT within the current window (clamped 0..1 so a
  // focus outside the window still produces a sane anchor).
  const frac = Math.min(1, Math.max(0, (focusT - v.t0) / oldW));
  const minW = minWindow(d);
  const maxW = d.hi - d.lo;
  // New width: clamp to [minW, full span].
  const newW = Math.min(maxW, Math.max(minW, oldW * factor));
  // Keep focusT at the same fraction: focusT = t0 + frac*newW.
  let t0 = focusT - frac * newW;
  let t1 = t0 + newW;
  // Slide the window back inside the domain without changing its width.
  if (t0 < d.lo) {
    t0 = d.lo;
    t1 = d.lo + newW;
  }
  if (t1 > d.hi) {
    t1 = d.hi;
    t0 = d.hi - newW;
  }
  if (t0 < d.lo) t0 = d.lo;
  return { t0, t1 };
}

/**
 * Pan by `dt` seconds (positive → window moves later), keeping the window WIDTH
 * fixed and clamping into the domain.
 */
export function panView(v: GanttView, dt: number, d: GanttDomain): GanttView {
  const w = Math.min(d.hi - d.lo, v.t1 - v.t0);
  let t0 = v.t0 + dt;
  let t1 = t0 + w;
  if (t0 < d.lo) {
    t0 = d.lo;
    t1 = d.lo + w;
  }
  if (t1 > d.hi) {
    t1 = d.hi;
    t0 = d.hi - w;
  }
  if (t0 < d.lo) t0 = d.lo;
  return { t0, t1 };
}

/** True when the view is (within epsilon) the full content range. */
export function isFit(v: GanttView, d: GanttDomain): boolean {
  const span = d.hi - d.lo;
  const eps = Math.max(1e-6, span * 1e-4);
  return Math.abs(v.t0 - d.lo) <= eps && Math.abs(v.t1 - d.hi) <= eps;
}

/**
 * Build a view from two arbitrary times (e.g. a minimap brush selection),
 * ordering them, clamping into the domain, and widening to the minimum window
 * if the selection is thinner than that (anchored on the selection centre).
 * Used for "drag a region on the minimap → zoom the gantt to it".
 */
export function clampWindow(a: number, b: number, d: GanttDomain): GanttView {
  let t0 = Math.max(d.lo, Math.min(a, b));
  let t1 = Math.min(d.hi, Math.max(a, b));
  const minW = minWindow(d);
  if (t1 - t0 < minW) {
    const c = (t0 + t1) / 2;
    t0 = Math.max(d.lo, c - minW / 2);
    t1 = Math.min(d.hi, t0 + minW);
    t0 = Math.max(d.lo, t1 - minW);
  }
  return { t0, t1 };
}
