// FingerprintZ.tsx — the damped Lissajous session "fingerprint".
//
// Port of compose.html `fpSVG` (256-266). A damped Lissajous identicon of the
// session: x = plan progress, y = drift. A tight harmonic knot reads as
// on-plan; a loose scribble reads as drifting; an outward sweep (grow) reads as
// diverging scope. The end DOT is the only status-earned mark — `--bad` failed
// / `--good` done / `--accent` live — so the encoding holds: KIND/identity is
// the calm currentColor line, STATUS is the single treatment dot.
//
// Reused at sizes 30 (⌘K picker rows), 58 (session header), 72 (drawer). The
// geometry is computed in the fixed 0 0 64 64 viewBox and fit-to-`size`.
//
// SIGNATURE IS FROZEN — do not change the props.

import { useMemo } from 'react';
import type { ZFingerprint, ZSessionStatus } from './adapter';

export interface FingerprintZProps {
  fp: ZFingerprint;
  status: ZSessionStatus;
  id: string;
  size: number;
}

const N = 800; // sample count (study fpSVG)

/**
 * Build the damped-Lissajous path `d` from the fingerprint params. Pure +
 * deterministic — same params always yield the same curve. Mirrors the study's
 * `T = o.T || 30` guard so a zero/absent duration can never divide by zero.
 */
function fingerprintPath(fp: ZFingerprint): string {
  const T = fp.T || 30;
  const d = fp.d || 0;
  const px = fp.px || 0;
  const pts: string[] = [];
  for (let i = 0; i <= N; i++) {
    const t = (T * i) / N;
    // m = amplitude modulation. grow → slow outward sweep (divergence);
    // corrAt → a one-time correction kink that damps the amplitude to .45;
    // otherwise a steady 1 (the body just rides the exponential damping `d`).
    const m = fp.grow
      ? 0.3 + (0.55 * t) / T
      : fp.corrAt != null
        ? t < fp.corrAt
          ? 1
          : t < fp.corrAt + 3
            ? 1 - ((t - fp.corrAt) / 3) * 0.55
            : 0.45
        : 1;
    const amp = 24 * m * Math.exp(-d * t);
    const x = 32 + amp * Math.sin(fp.fx * t + px);
    const y = 32 + amp * Math.sin(fp.fy * t);
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  return `M${pts.join(' L')}`;
}

export function FingerprintZ({ fp, status, id, size }: FingerprintZProps) {
  // Memoise the 800-point path on the params (FingerprintZ is rendered in
  // every ⌘K picker row, so recomputing per keystroke would be wasteful).
  const { d, end } = useMemo(() => {
    const path = fingerprintPath(fp);
    const T = fp.T || 30;
    const damp = fp.d || 0;
    const px = fp.px || 0;
    // End point = the last sample (i = N). Recompute it directly rather than
    // re-parsing the path string.
    const t = T;
    const m = fp.grow
      ? 0.3 + (0.55 * t) / T
      : fp.corrAt != null
        ? t < fp.corrAt
          ? 1
          : t < fp.corrAt + 3
            ? 1 - ((t - fp.corrAt) / 3) * 0.55
            : 0.45
        : 1;
    const amp = 24 * m * Math.exp(-damp * t);
    return {
      d: path,
      end: {
        x: 32 + amp * Math.sin(fp.fx * t + px),
        y: 32 + amp * Math.sin(fp.fy * t),
      },
    };
  }, [fp]);

  // STATUS = treatment: the single status-earned colour, only on the end dot.
  const dot =
    status === 'failed'
      ? 'var(--bad)'
      : status === 'done'
        ? 'var(--good)'
        : 'var(--accent)';

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label={`fingerprint ${id}`}
      focusable="false"
    >
      {/* The identity line — calm, single currentColor hairline. Non-scaling so
          the 0.9px stroke stays a hairline at size 30 as well as 72. */}
      <path
        d={d}
        fill="none"
        stroke="currentColor"
        strokeWidth={0.9}
        opacity={0.7}
        vectorEffect="non-scaling-stroke"
      />
      <circle cx={end.x.toFixed(1)} cy={end.y.toFixed(1)} r={2.4} fill={dot} />
    </svg>
  );
}
