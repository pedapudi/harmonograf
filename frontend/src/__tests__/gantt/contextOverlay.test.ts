import { describe, expect, it } from 'vitest';
import {
  computeContextBandGeom,
  contextColorForRatio,
  contextRatio,
  formatTokens,
} from '../../gantt/contextOverlay';
import type { ContextWindowSample } from '../../gantt/types';

describe('contextColorForRatio', () => {
  it('buckets low usage as green', () => {
    expect(contextColorForRatio(0).bucket).toBe('low');
    expect(contextColorForRatio(0.25).bucket).toBe('low');
    expect(contextColorForRatio(0.4999).bucket).toBe('low');
  });

  it('buckets 50-75% as yellow/warn', () => {
    expect(contextColorForRatio(0.5).bucket).toBe('warn');
    expect(contextColorForRatio(0.6).bucket).toBe('warn');
    expect(contextColorForRatio(0.7499).bucket).toBe('warn');
  });

  it('buckets 75-90% as orange/high', () => {
    expect(contextColorForRatio(0.75).bucket).toBe('high');
    expect(contextColorForRatio(0.85).bucket).toBe('high');
    expect(contextColorForRatio(0.8999).bucket).toBe('high');
  });

  it('buckets >=90% as red/critical', () => {
    expect(contextColorForRatio(0.9).bucket).toBe('critical');
    expect(contextColorForRatio(0.99).bucket).toBe('critical');
    expect(contextColorForRatio(1).bucket).toBe('critical');
    expect(contextColorForRatio(2).bucket).toBe('critical');
  });

  it('treats NaN / negative / non-finite as low', () => {
    expect(contextColorForRatio(NaN).bucket).toBe('low');
    expect(contextColorForRatio(-0.5).bucket).toBe('low');
    expect(contextColorForRatio(Number.NEGATIVE_INFINITY).bucket).toBe('low');
  });

  it('returns distinct hex fill/stroke per bucket', () => {
    const low = contextColorForRatio(0);
    const critical = contextColorForRatio(1);
    expect(low.fill).not.toBe(critical.fill);
    expect(low.stroke).not.toBe(critical.stroke);
    expect(low.fill).toMatch(/^#[0-9a-f]{6}$/i);
    expect(low.stroke).toMatch(/^#[0-9a-f]{6}$/i);
  });
});

describe('contextRatio', () => {
  it('returns 0 for zero limit (unknown)', () => {
    expect(contextRatio(1000, 0)).toBe(0);
    expect(contextRatio(0, 0)).toBe(0);
  });

  it('clamps to [0, 1]', () => {
    expect(contextRatio(50, 100)).toBeCloseTo(0.5);
    expect(contextRatio(200, 100)).toBe(1);
    expect(contextRatio(-10, 100)).toBe(0);
  });

  it('handles non-finite tokens', () => {
    expect(contextRatio(NaN, 100)).toBe(0);
    expect(contextRatio(Infinity, 100)).toBe(0);
  });
});

describe('formatTokens', () => {
  it('formats under 1000 as integer', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(42)).toBe('42');
    expect(formatTokens(999)).toBe('999');
  });

  it('formats thousands with one decimal', () => {
    expect(formatTokens(1500)).toBe('1.5k');
    expect(formatTokens(9999)).toBe('10.0k');
  });

  it('formats tens of thousands without decimal', () => {
    expect(formatTokens(10_000)).toBe('10k');
    expect(formatTokens(32_768)).toBe('33k');
  });

  it('formats millions with one decimal and M suffix', () => {
    expect(formatTokens(2_000_000)).toBe('2.0M');
    expect(formatTokens(1_250_000)).toBe('1.3M');
  });

  it('clamps negative and non-finite to 0', () => {
    expect(formatTokens(-1)).toBe('0');
    expect(formatTokens(NaN)).toBe('0');
  });
});

// ── Band geometry ─────────────────────────────────────────────────────────
//
// All tests use an identity msToPx (x = ms) and a row that spans [100, 140)
// with bandHeight 20. That makes ratio=1 land at y = rowBottom - bandHeight =
// 138 - 20 = 118, and ratio=0 at y = 138 (the -2 offset in rowBottom comes
// from the renderer's own 2px inset).

const rowTopY = 100;
const rowHeight = 40;
const bandHeight = 20;
// With the renderer's own `- 2` inset, baselineY = rowTopY + rowHeight - 2 = 138.
const baselineY = 138;
const topMaxY = baselineY - bandHeight; // 118

function yForRatio(ratio: number): number {
  return baselineY - (baselineY - topMaxY) * ratio;
}

const identityInput = (samples: ContextWindowSample[], viewport: [number, number]) => ({
  samples,
  viewportStartMs: viewport[0],
  viewportEndMs: viewport[1],
  msToPx: (ms: number) => ms,
  leftClipPx: viewport[0],
  rightClipPx: viewport[1],
  rowTopY,
  rowHeight,
  bandHeight,
});

describe('computeContextBandGeom', () => {
  it('returns null for empty samples', () => {
    expect(
      computeContextBandGeom(identityInput([], [0, 100])),
    ).toBeNull();
  });

  it('returns null when right clip <= left clip', () => {
    const out = computeContextBandGeom({
      ...identityInput([{ tMs: 10, tokens: 50, limitTokens: 100 }], [0, 100]),
      leftClipPx: 50,
      rightClipPx: 50,
    });
    expect(out).toBeNull();
  });

  it('carries a pre-viewport sample through the left edge', () => {
    // Sample at t=0 sits before the viewport [50, 150]; its ratio should
    // seed the polygon at the left clip.
    const out = computeContextBandGeom(
      identityInput(
        [{ tMs: 0, tokens: 30, limitTokens: 100 }],
        [50, 150],
      ),
    )!;
    expect(out.top.length).toBeGreaterThanOrEqual(2);
    expect(out.top[0].x).toBe(50);
    expect(out.top[0].y).toBeCloseTo(yForRatio(0.3));
    // The tail should extend all the way to the right clip.
    expect(out.top[out.top.length - 1].x).toBe(150);
    expect(out.maxRatio).toBeCloseTo(0.3);
    expect(out.lastRatio).toBeCloseTo(0.3);
  });

  it('produces step-function geometry between samples', () => {
    // Two samples: 20% at t=10, 80% at t=60, viewport [0, 100].
    const out = computeContextBandGeom(
      identityInput(
        [
          { tMs: 10, tokens: 20, limitTokens: 100 },
          { tMs: 60, tokens: 80, limitTokens: 100 },
        ],
        [0, 100],
      ),
    )!;
    // The seed sits at the left clip with the first sample's ratio (no
    // earlier carry-over), then rises at x=60 to the second sample's ratio.
    expect(out.top[0]).toEqual({ x: 0, y: yForRatio(0.2) });
    // Find the rise at x=60: the step is two consecutive points sharing x=60,
    // first holding the prior y, then jumping to the new y.
    const riseIdx = out.top.findIndex(
      (p, i) => i > 0 && p.x === 60 && out.top[i - 1].x < 60,
    );
    expect(riseIdx).toBeGreaterThan(0);
    const before = out.top[riseIdx];
    const after = out.top[riseIdx + 1];
    expect(before.x).toBe(60);
    expect(after.x).toBe(60);
    expect(before.y).toBeCloseTo(yForRatio(0.2));
    expect(after.y).toBeCloseTo(yForRatio(0.8));
    // Tail extended to right clip at the new y.
    const tail = out.top[out.top.length - 1];
    expect(tail.x).toBe(100);
    expect(tail.y).toBeCloseTo(yForRatio(0.8));
  });

  it('reports the peak ratio across the visible window', () => {
    const out = computeContextBandGeom(
      identityInput(
        [
          { tMs: 0, tokens: 10, limitTokens: 100 },
          { tMs: 20, tokens: 95, limitTokens: 100 }, // peak
          { tMs: 40, tokens: 30, limitTokens: 100 },
        ],
        [0, 100],
      ),
    )!;
    expect(out.maxRatio).toBeCloseTo(0.95);
    // lastRatio is the most recent sample's ratio, not the peak.
    expect(out.lastRatio).toBeCloseTo(0.3);
  });

  it('ignores samples beyond the right edge of the viewport', () => {
    const out = computeContextBandGeom(
      identityInput(
        [
          { tMs: 10, tokens: 40, limitTokens: 100 },
          { tMs: 200, tokens: 99, limitTokens: 100 }, // outside [0, 100]
        ],
        [0, 100],
      ),
    )!;
    expect(out.maxRatio).toBeCloseTo(0.4);
    expect(out.lastRatio).toBeCloseTo(0.4);
  });

  it('pins the polygon baseline to the row bottom', () => {
    const out = computeContextBandGeom(
      identityInput(
        [{ tMs: 10, tokens: 50, limitTokens: 100 }],
        [0, 100],
      ),
    )!;
    expect(out.baselineY).toBe(baselineY);
  });

  it('treats zero limit as unknown (ratio 0)', () => {
    const out = computeContextBandGeom(
      identityInput(
        [{ tMs: 10, tokens: 50_000, limitTokens: 0 }],
        [0, 100],
      ),
    )!;
    expect(out.maxRatio).toBe(0);
    // Polyline y should sit on the baseline when ratio is 0.
    expect(out.top[0].y).toBe(baselineY);
  });
});
