// svgUtils.ts — shared pure helpers for the zicato SVG renderers. Ported from
// compose.html (KIND/lerpKeys/lcg 161-176, brand 149-157, trackGeom 448-449,
// judgeBeats 171-176). No React, no data fetching — just geometry + color math
// the figure components import. `colorVar`/`severityToValue` are owned by
// adapter.ts and re-exported here so there is exactly one definition of each.

import type { ZKindToken, ZGfClass, ZSpan, ZJudges, ZSteer } from './adapter';
import { colorVar, severityToValue } from './adapter';

export { colorVar, severityToValue };

/**
 * The hue a goldfive steering arrow takes, keyed off the trigger severity:
 * critical → --bad, warning → --caution, anything else (info / unspecified) →
 * the goldfive-refine token. Keeps steering distinct from transfer/delegation
 * edges while still reading as "a correction".
 */
export function steerColor(s: Pick<ZSteer, 'severity'>): string {
  const sev = (s.severity || '').toLowerCase();
  if (sev === 'critical') return 'var(--bad)';
  if (sev === 'warning' || sev === 'warn') return 'var(--caution)';
  return 'var(--hg-gf-refine)';
}

/** KIND = hue. `'llm-call'` → `var(--hg-kind-llm-call)`. (compose.html:161) */
export const KIND = (k: ZKindToken): string => `var(--hg-kind-${k})`;

/** `--hg-gf-*` for a goldfive category. */
export function gfVar(gf: Exclude<ZGfClass, null>): string {
  return `var(--hg-gf-${gf})`;
}

/**
 * The fill a span bar takes (KIND-first encoding):
 *   failed → --bad, goldfive → gfVar, planned → CSS-driven (KIND fallback),
 *   else the kind hue.
 * (compose.html ganttSVG fill decision, 85.)
 */
export function statusFill(sp: ZSpan): string {
  if (sp.status === 'failed') return 'var(--bad)';
  if (sp.gf) return gfVar(sp.gf);
  return KIND(sp.kind);
}

/**
 * Piecewise-linear interpolation over `[[t, v], …]` keyframes. (compose.html
 * 162-164.) Clamps to the endpoints outside the keyframe range.
 */
export function lerpKeys(keys: [number, number][], t: number): number {
  if (keys.length === 0) return 0;
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) {
    if (t <= keys[i][0]) {
      const [t0, v0] = keys[i - 1];
      const [t1, v1] = keys[i];
      if (t1 === t0) return v1;
      return v0 + ((v1 - v0) * (t - t0)) / (t1 - t0);
    }
  }
  return keys[keys.length - 1][1];
}

/**
 * Deterministic LCG PRNG seeded by `seed`. Returns a `() => number` in [0,1).
 * Used for the seismograph's drift jitter so the trace is stable across renders
 * (NEVER Math.random in render). (compose.html:165.)
 */
export function lcg(seed: number): () => number {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
}

/**
 * Time→x scale shared by the seismograph + ladder so a reading at t lines up
 * vertically. (compose.html trackGeom 448-449.) `padL` defaults narrow for tiny
 * widths. Callers may override padL/padR.
 */
export function timeScale(
  T: number,
  W: number,
  padL = W < 400 ? 40 : 76,
  padR = 14,
): { padL: number; padR: number; X: (t: number) => number } {
  const denom = T > 0 ? T : 1;
  return {
    padL,
    padR,
    X: (t: number) => padL + ((W - padL - padR) * t) / denom,
  };
}

/**
 * A stable, DOM-safe id derived from a seed string. Used for per-instance
 * `<defs>` gradient ids (ChordZ) so multiple chords on one page don't collide.
 */
export function uniqueId(seed: string): string {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return `zk-${h.toString(36)}`;
}

/**
 * The harmonograf α-mark path 'd': ONE period of a 2:3 Lissajous mirrored about
 * the vertical axis (reads as a lowercase α). (compose.html hgAlphaPath 149-152.)
 * Geometry only — BrandMark renders the JSX around it.
 */
export function hgAlphaPath(cx: number, cy: number, A: number, B: number): string {
  const pts: string[] = [];
  for (let i = 0; i <= 240; i++) {
    const t = (2 * Math.PI * i) / 240;
    pts.push(
      `${(cx - A * Math.cos(2 * t)).toFixed(2)},${(
        cy +
        B * Math.sin(3 * t)
      ).toFixed(2)}`,
    );
  }
  return `M${pts.join(' L')} Z`;
}

/**
 * The study's default seismograph lanes. The adapter overrides this with the
 * real busy lanes when available; this is the empty-data fallback.
 */
export const SEISMO_LANES_DEFAULT = ['coder', 'reviewer', 'planner'] as const;

/**
 * The judge heartbeat beats DERIVED from the drift the judges watch, so every
 * firing mark sits above the drift reading that earned it. (compose.html
 * judgeBeats 171-176.) TOL=8, step 2.6s; ok / warn(▲) / crit(✕).
 */
export function judgeBeats(
  judges: ZJudges,
  lanes: string[],
  T: number,
  now: number,
): [number, 'ok' | 'warn' | 'crit'][] {
  const TOL = 8;
  const beats: [number, 'ok' | 'warn' | 'crit'][] = [];
  const end = Math.min(now, T);
  for (let t = 2; t <= end + 0.001; t += 2.6) {
    let d = 0;
    for (const a of lanes) {
      const keys = judges[a];
      if (keys && keys.length) d = Math.max(d, lerpKeys(keys, t));
    }
    beats.push([t, d >= TOL * 1.5 ? 'crit' : d >= TOL ? 'warn' : 'ok']);
  }
  return beats;
}
