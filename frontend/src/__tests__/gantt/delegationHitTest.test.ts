import { describe, expect, it, vi } from 'vitest';
import { SessionStore } from '../../gantt/index';
import type { DelegationRecord } from '../../gantt/index';
import { GanttRenderer } from '../../gantt/renderer';

// The hit-test depends only on DelegationEdgeLayout geometry + pointer
// coordinates. We don't exercise the full draw pipeline (jsdom has no
// canvas 2D context); instead, we seed known layouts via the renderer's
// _seedDelegationLayoutsForTesting shim and assert the hit-test returns
// the expected record for points-near-bezier and null for far points.
//
// Geometry under test — bezier P0..P3 matching drawDelegations():
//   P0 = (x, srcY)
//   P1 = (x + off, srcY)
//   P2 = (x + off, tgtY)
//   P3 = (x, tgtY)
// For (x=500, srcY=100, tgtY=200, off=24), the midpoint of the curve
// evaluates to roughly (518, 150). Points within ~6px of any sampled
// segment should hit; points far from the curve should miss.

function mkRecord(
  seq: number,
  overrides: Partial<DelegationRecord> = {},
): DelegationRecord {
  return {
    seq,
    fromAgentId: 'coord',
    toAgentId: `sub_${seq}`,
    taskId: `t-${seq}`,
    invocationId: `inv-${seq}`,
    observedAtMs: 1000 * (seq + 1),
    ...overrides,
  };
}

function mkRenderer(): GanttRenderer {
  return new GanttRenderer(new SessionStore());
}

describe('GanttRenderer.hitTestDelegation', () => {
  it('returns null when no delegation layouts are cached', () => {
    const r = mkRenderer();
    expect(r.hitTestDelegation(500, 150)).toBeNull();
  });

  it('hits a point lying on the curve midpoint', () => {
    const r = mkRenderer();
    const rec = mkRecord(0);
    r._seedDelegationLayoutsForTesting([
      { seq: rec.seq, record: rec, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
    ]);
    // Midpoint of the bezier is at (x + 3/4 * off, (srcY+tgtY)/2) = (518, 150).
    expect(r.hitTestDelegation(518, 150)).toBe(rec);
  });

  it('hits a point near an endpoint', () => {
    const r = mkRenderer();
    const rec = mkRecord(0);
    r._seedDelegationLayoutsForTesting([
      { seq: rec.seq, record: rec, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
    ]);
    // A tolerance of 6px means (501, 101) sits on the source-anchor region.
    expect(r.hitTestDelegation(501, 101)).toBe(rec);
    expect(r.hitTestDelegation(501, 199)).toBe(rec);
  });

  it('misses when the pointer is far from every curve', () => {
    const r = mkRenderer();
    const rec = mkRecord(0);
    r._seedDelegationLayoutsForTesting([
      { seq: rec.seq, record: rec, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
    ]);
    // Point well outside the bezier's bounding bump.
    expect(r.hitTestDelegation(100, 100)).toBeNull();
    // Same-y as the source but off to the right of the curve's rightmost x.
    expect(r.hitTestDelegation(600, 100)).toBeNull();
  });

  it('returns the most recent record when multiple curves overlap', () => {
    const r = mkRenderer();
    const older = mkRecord(0);
    const newer = mkRecord(1);
    // Two edges sharing the same geometry — the back-to-front walk in
    // hitTestDelegation must return the `newer` one since it was appended
    // later in the Registry's array.
    r._seedDelegationLayoutsForTesting([
      { seq: older.seq, record: older, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
      { seq: newer.seq, record: newer, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
    ]);
    expect(r.hitTestDelegation(518, 150)).toBe(newer);
  });

  it('bbox-rejects points outside the curve envelope without sampling', () => {
    const r = mkRenderer();
    const rec = mkRecord(0);
    r._seedDelegationLayoutsForTesting([
      { seq: rec.seq, record: rec, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
    ]);
    // x > x + off + tol (≈ 530): out of bbox.
    expect(r.hitTestDelegation(540, 150)).toBeNull();
    // y < srcY - tol (≈ 94): out of bbox.
    expect(r.hitTestDelegation(510, 50)).toBeNull();
  });

  it('picks the correct record when two edges are near each other', () => {
    const r = mkRenderer();
    const a = mkRecord(0);
    const b = mkRecord(1);
    r._seedDelegationLayoutsForTesting([
      { seq: a.seq, record: a, x: 200, srcY: 100, tgtY: 200, curveOffset: 24 },
      { seq: b.seq, record: b, x: 400, srcY: 300, tgtY: 400, curveOffset: 24 },
    ]);
    expect(r.hitTestDelegation(218, 150)).toBe(a);
    expect(r.hitTestDelegation(418, 350)).toBe(b);
    // Midway between the two edges — far from both curves.
    expect(r.hitTestDelegation(300, 250)).toBeNull();
  });
});

describe('GanttRenderer delegation hover/click callbacks', () => {
  it('emits onDelegationClick when a click lands on a cached edge', () => {
    const store = new SessionStore();
    const onDelegationClick = vi.fn();
    const r = new GanttRenderer(store, { onDelegationClick });
    const rec = mkRecord(0);
    r._seedDelegationLayoutsForTesting([
      { seq: rec.seq, record: rec, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
    ]);
    r.handleClick(518, 150);
    expect(onDelegationClick).toHaveBeenCalledTimes(1);
    expect(onDelegationClick.mock.calls[0][0]).toBe(rec);
  });

  it('emits onDelegationHoverChange when a pointer moves onto + off an edge', () => {
    const store = new SessionStore();
    const onDelegationHoverChange = vi.fn();
    const r = new GanttRenderer(store, { onDelegationHoverChange });
    const rec = mkRecord(0);
    r._seedDelegationLayoutsForTesting([
      { seq: rec.seq, record: rec, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
    ]);
    r.handlePointerMove(518, 150);
    r.handlePointerMove(100, 100);
    const calls = onDelegationHoverChange.mock.calls;
    // First call: hover on; record is the injected one.
    expect(calls[0][0]?.record).toBe(rec);
    // Final call: hover cleared → null.
    expect(calls[calls.length - 1][0]).toBeNull();
  });

  it('does not fire onDelegationClick for clicks off every edge', () => {
    const store = new SessionStore();
    const onDelegationClick = vi.fn();
    const r = new GanttRenderer(store, { onDelegationClick });
    const rec = mkRecord(0);
    r._seedDelegationLayoutsForTesting([
      { seq: rec.seq, record: rec, x: 500, srcY: 100, tgtY: 200, curveOffset: 24 },
    ]);
    r.handleClick(100, 100);
    expect(onDelegationClick).not.toHaveBeenCalled();
  });
});
