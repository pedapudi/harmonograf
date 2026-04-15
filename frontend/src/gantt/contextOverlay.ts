// Pure helpers for the context-window Gantt overlay (task #3).
//
// Kept separate from renderer.ts so the color selector and geometry math are
// unit-testable without spinning up a canvas. The renderer imports these and
// hands them viewport-projected coordinates; nothing here touches DOM,
// RequestAnimationFrame, or the SessionStore.

import type { ContextWindowSample } from './types';

export type ContextBucket = 'low' | 'warn' | 'high' | 'critical';

export interface ContextHeatColor {
  fill: string;    // area-fill color (used at low alpha under spans)
  stroke: string;  // edge-line color (higher alpha, drawn on the band top)
  bucket: ContextBucket;
}

// Stepped palette — green / yellow / orange / red at the thresholds called
// out in the task #3 spec. A stepped (not interpolated) gradient keeps the
// boundaries legible both on the canvas band and on the DOM header chip, and
// makes the color selector trivially unit-testable.
const CTX_LOW: ContextHeatColor = {
  fill: '#2e7d32',
  stroke: '#66bb6a',
  bucket: 'low',
};
const CTX_WARN: ContextHeatColor = {
  fill: '#f9a825',
  stroke: '#ffd54f',
  bucket: 'warn',
};
const CTX_HIGH: ContextHeatColor = {
  fill: '#ef6c00',
  stroke: '#ffb74d',
  bucket: 'high',
};
const CTX_CRITICAL: ContextHeatColor = {
  fill: '#c62828',
  stroke: '#ff6b6b',
  bucket: 'critical',
};

// Ratio → (fill, stroke, bucket). NaN / negative / non-finite → 'low'.
export function contextColorForRatio(ratio: number): ContextHeatColor {
  if (!Number.isFinite(ratio) || ratio < 0.5) return CTX_LOW;
  if (ratio < 0.75) return CTX_WARN;
  if (ratio < 0.9) return CTX_HIGH;
  return CTX_CRITICAL;
}

// Safe ratio. A zero limit means "unknown" → 0 so the color falls in the low
// bucket and the geometry collapses to the baseline.
export function contextRatio(tokens: number, limit: number): number {
  if (!(limit > 0)) return 0;
  if (!Number.isFinite(tokens) || tokens < 0) return 0;
  return Math.min(1, tokens / limit);
}

// Compact token display for the header chip + the hover tooltip.
export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 10_000) return Math.round(n / 1_000) + 'k';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
  return String(Math.round(n));
}

// ── Band geometry ───────────────────────────────────────────────────────
// The overlay is rendered as a filled polygon hugging the row's bottom
// baseline, with its upper edge tracing the ratio at each sample.
//
// Coordinate model:
//   • baselineY is the y the fill rests on (row bottom, in CSS pixels).
//   • topMaxY is baselineY - bandHeight, reserved for ratio == 1.
//   • y = baselineY - (baselineY - topMaxY) * ratio    (linear in ratio)
//
// Samples carry the tokens + limit at a specific session-relative timestamp.
// Context-window usage is a step function in time (tokens hold until the
// next heartbeat), so the polyline is drawn as horizontal step segments:
// each sample contributes a horizontal segment from its own x to the next
// sample's x at its own y. The final sample extends to the right clip so
// the band reaches the live edge.
//
// The returned polyline is the UPPER edge only. The renderer closes the
// polygon by walking back along the baseline for the fill pass.

export interface ContextBandPoint {
  x: number;
  y: number;
}

export interface ContextBandGeom {
  // Step-function upper edge. Consecutive pairs alternate between the "rise"
  // at a sample x (vertical move from the prior y to the new y) and the
  // "run" (horizontal move to the next sample's x). For N samples visible,
  // `top` has 2N points. The first point sits at leftClipPx and the last at
  // rightClipPx.
  top: ContextBandPoint[];
  baselineY: number;
  // Peak ratio within the visible range — the renderer uses this to select
  // a single dominant color for the entire visible band so the fill reads
  // as a heatmap cell rather than a multi-tone gradient (which costs a
  // gradient object per row per frame).
  maxRatio: number;
  // Ratio at the current right edge of the visible window — used by the
  // header chip when it wants to mirror the canvas band color.
  lastRatio: number;
}

export interface BandGeomInput {
  samples: readonly ContextWindowSample[];
  viewportStartMs: number;
  viewportEndMs: number;
  msToPx: (ms: number) => number;
  leftClipPx: number;
  rightClipPx: number;
  rowTopY: number;
  rowHeight: number;
  // How much of the row (from the bottom up) the overlay fills at ratio=1.
  bandHeight: number;
}

export function computeContextBandGeom(
  input: BandGeomInput,
): ContextBandGeom | null {
  const {
    samples,
    viewportStartMs,
    viewportEndMs,
    msToPx,
    leftClipPx,
    rightClipPx,
    rowTopY,
    rowHeight,
    bandHeight,
  } = input;
  if (samples.length === 0) return null;
  if (rightClipPx <= leftClipPx) return null;

  const baselineY = rowTopY + rowHeight - 2;
  const band = Math.max(2, Math.min(bandHeight, rowHeight - 4));
  const topMaxY = baselineY - band;

  // Locate the last sample whose time is at or before the viewport start.
  // That sample's tokens/limit represent the state at the left edge (step
  // function carry-over). Fall back to the first sample if none precedes the
  // viewport.
  let startIdx = -1;
  for (let i = 0; i < samples.length; i++) {
    if (samples[i].tMs <= viewportStartMs) startIdx = i;
    else break;
  }
  if (startIdx < 0) startIdx = 0;

  const ratioToY = (ratio: number): number =>
    baselineY - (baselineY - topMaxY) * ratio;

  const pts: ContextBandPoint[] = [];
  let maxRatio = 0;
  let lastRatio = 0;

  // Seed at the left clip with the carry-over sample's ratio.
  const seed = samples[startIdx];
  const seedRatio = contextRatio(seed.tokens, seed.limitTokens);
  maxRatio = seedRatio;
  lastRatio = seedRatio;
  pts.push({ x: leftClipPx, y: ratioToY(seedRatio) });

  // Walk forward; each subsequent sample introduces a vertical rise at its x
  // (extending the prior horizontal segment to that x first) followed by a
  // new y carried rightward.
  for (let i = startIdx + 1; i < samples.length; i++) {
    const s = samples[i];
    if (s.tMs > viewportEndMs) break;
    const rawX = msToPx(s.tMs);
    if (rawX <= leftClipPx) {
      // Sample still sits at or left of the clip — treat it as a carry-over
      // update to the seed ratio without producing geometry.
      const r = contextRatio(s.tokens, s.limitTokens);
      if (r > maxRatio) maxRatio = r;
      lastRatio = r;
      pts[0].y = ratioToY(r);
      continue;
    }
    const x = Math.min(rightClipPx, rawX);
    const r = contextRatio(s.tokens, s.limitTokens);
    if (r > maxRatio) maxRatio = r;
    // Close the prior horizontal run at (x, prev.y), then rise to (x, newY).
    const prev = pts[pts.length - 1];
    pts.push({ x, y: prev.y });
    pts.push({ x, y: ratioToY(r) });
    lastRatio = r;
    if (x >= rightClipPx) break;
  }

  // Extend the final segment to the right clip so the polygon reaches the
  // live edge (the tokens are still "this" until the next heartbeat arrives).
  const tail = pts[pts.length - 1];
  if (tail.x < rightClipPx) {
    pts.push({ x: rightClipPx, y: tail.y });
  }

  return { top: pts, baselineY, maxRatio, lastRatio };
}
