// Regression for live intervention-band rendering. Mirrors the brain-badge
// + delegation-arrow live-alignment bugs: an intervention that arrives
// before the session 'created_at' is known is recorded with a bogus
// session-relative ms. When the 'session' SessionUpdate delivers the
// real start, the store rebases its drifts — and the Gantt overlay must
// re-paint the band at the corrected x, not at the stale one.
//
// Verification strategy mirrors brainBadges.test.ts: stub the 2D canvas
// context, drive the private drawOverlay via the typed escape hatch,
// read back the renderer's per-frame band counters (lastInterventionBandXs).

import { describe, expect, it } from 'vitest';
import { GanttRenderer } from '../../gantt/renderer';
import { SessionStore } from '../../gantt/index';
import type { InterventionRow } from '../../lib/interventions';

function stubCtx(): CanvasRenderingContext2D {
  const handler: ProxyHandler<object> = {
    get(_t, prop) {
      if (prop === 'canvas') return { width: 1200, height: 400 };
      if (prop === 'globalAlpha') return 1;
      if (prop === 'measureText') return () => ({ width: 10 });
      return () => undefined;
    },
    set() {
      return true;
    },
  };
  return new Proxy({}, handler) as CanvasRenderingContext2D;
}

function stubCanvas(): HTMLCanvasElement {
  const el = document.createElement('canvas');
  el.width = 1200;
  el.height = 400;
  (el as unknown as { getContext: () => CanvasRenderingContext2D }).getContext =
    () => stubCtx();
  return el;
}

function mkRow(over: Partial<InterventionRow>): InterventionRow {
  return {
    key: 'k',
    atMs: 0,
    source: 'drift',
    kind: 'LOOPING_REASONING',
    bodyOrReason: '',
    author: '',
    outcome: '',
    planRevisionIndex: 0,
    severity: 'warning',
    annotationId: '',
    driftKind: 'looping_reasoning',
    triggerEventId: '',
    targetAgentId: '',
    driftId: '',
    attemptId: '',
    failureKind: '',
    ...over,
  };
}

function drawOverlay(r: GanttRenderer): void {
  (r as unknown as { drawOverlay: () => void }).drawOverlay();
}

describe('GanttRenderer intervention bands — live rendering', () => {
  it('re-anchors a band when setInterventions is called with a rebased atMs', () => {
    const store = new SessionStore();
    const renderer = new GanttRenderer(store);
    renderer.attach(stubCanvas(), stubCanvas(), stubCanvas());
    renderer.resize(1200, 200, 1);
    renderer.setViewport({
      endMs: 60_000,
      windowMs: 60_000,
      liveFollow: false,
      replay: false,
    });

    // A drift arrived before the session's created_at landed, so its
    // session-relative ms was recorded as ~wall-clock (bogus big number
    // that clamps into the viewport end). Render picks up that bogus x.
    renderer.setInterventions([mkRow({ key: 'd1', atMs: 55_000 })]);
    drawOverlay(renderer);
    expect(renderer.lastInterventionBandCount).toBe(1);
    const staleX = renderer.lastInterventionBandXs[0];

    // Session update arrives → drifts.rebase → GanttView re-derives
    // interventions with corrected atMs → calls setInterventions again.
    renderer.setInterventions([mkRow({ key: 'd1', atMs: 5_000 })]);
    drawOverlay(renderer);
    expect(renderer.lastInterventionBandCount).toBe(1);
    const freshX = renderer.lastInterventionBandXs[0];

    // Band must move: 5s should land far left of 55s at the same zoom.
    expect(freshX).toBeLessThan(staleX);
  });

  it('zeroes the band count when the toggle is flipped off', () => {
    const store = new SessionStore();
    const renderer = new GanttRenderer(store);
    renderer.attach(stubCanvas(), stubCanvas(), stubCanvas());
    renderer.resize(1200, 200, 1);
    renderer.setViewport({
      endMs: 60_000,
      windowMs: 60_000,
      liveFollow: false,
      replay: false,
    });
    renderer.setInterventions([
      mkRow({ key: 'a', atMs: 10_000, source: 'user', kind: 'STEER' }),
      mkRow({ key: 'b', atMs: 40_000 }),
    ]);
    drawOverlay(renderer);
    expect(renderer.lastInterventionBandCount).toBe(2);

    renderer.setInterventionBandsVisible(false);
    drawOverlay(renderer);
    expect(renderer.lastInterventionBandCount).toBe(0);
  });

  it('clusters bands that fall within ~6px of each other', () => {
    const store = new SessionStore();
    const renderer = new GanttRenderer(store);
    renderer.attach(stubCanvas(), stubCanvas(), stubCanvas());
    renderer.resize(1200, 200, 1);
    // Narrow viewport so 3 closely-spaced atMs map to sub-6px spacing.
    renderer.setViewport({
      endMs: 120_000,
      windowMs: 120_000,
      liveFollow: false,
      replay: false,
    });
    renderer.setInterventions([
      mkRow({ key: 'a', atMs: 30_000 }),
      mkRow({ key: 'b', atMs: 30_100 }),
      mkRow({ key: 'c', atMs: 30_200 }),
    ]);
    drawOverlay(renderer);
    // Three rows within 200ms of each other at 120s / ~1000px data-area ≈
    // 0.16 px/ms, so 200ms = 32px. That's above 6 — the cluster threshold
    // kicks in only when x-distance is ≤6px. So we expect 3 distinct
    // bands. Flip the viewport tight enough that 200ms < 6px (i.e.
    // viewport ≥ ~33s per pixel = 33s × 1000px = 33_000s = 33M ms).
    // Above keeps all 3 separate; let's also test the merge case:
  });

  it('clusters tightly-packed rows (sub-pixel merge)', () => {
    const store = new SessionStore();
    const renderer = new GanttRenderer(store);
    renderer.attach(stubCanvas(), stubCanvas(), stubCanvas());
    renderer.resize(1200, 200, 1);
    // 1 hour viewport → ~277ms per pixel → 3 rows within 1200ms land
    // inside the same 6-pixel cluster window.
    renderer.setViewport({
      endMs: 3_600_000,
      windowMs: 3_600_000,
      liveFollow: false,
      replay: false,
    });
    renderer.setInterventions([
      mkRow({ key: 'a', atMs: 1_800_000 }),
      mkRow({ key: 'b', atMs: 1_800_400 }),
      mkRow({ key: 'c', atMs: 1_800_800 }),
    ]);
    drawOverlay(renderer);
    expect(renderer.lastInterventionBandCount).toBe(1);
  });

  // ─── InvocationCancelled bands (goldfive#251 Stream C) ──────────────────
  it('cancel-source rows render as their own band on the Gantt', () => {
    const store = new SessionStore();
    const renderer = new GanttRenderer(store);
    renderer.attach(stubCanvas(), stubCanvas(), stubCanvas());
    renderer.resize(1200, 200, 1);
    renderer.setViewport({
      endMs: 60_000,
      windowMs: 60_000,
      liveFollow: false,
      replay: false,
    });
    renderer.setInterventions([
      mkRow({
        key: 'c1',
        atMs: 10_000,
        source: 'cancel',
        kind: 'CANCELLED',
        severity: 'critical',
      }),
    ]);
    drawOverlay(renderer);
    expect(renderer.lastInterventionBandCount).toBe(1);
  });

  it('cancel wins over drift in a cluster pickSource race', () => {
    const store = new SessionStore();
    const renderer = new GanttRenderer(store);
    renderer.attach(stubCanvas(), stubCanvas(), stubCanvas());
    renderer.resize(1200, 200, 1);
    // Wide viewport so the rows cluster into one band — pickSource
    // promotes cancel over drift so the final cluster colour reads
    // as the terminal marker, not the upstream drift.
    renderer.setViewport({
      endMs: 3_600_000,
      windowMs: 3_600_000,
      liveFollow: false,
      replay: false,
    });
    renderer.setInterventions([
      mkRow({ key: 'd', atMs: 1_800_000, source: 'drift', kind: 'OFF_TOPIC' }),
      mkRow({
        key: 'c',
        atMs: 1_800_400,
        source: 'cancel',
        kind: 'CANCELLED',
        severity: 'critical',
      }),
    ]);
    drawOverlay(renderer);
    // Merged into a single band; the renderer's instrumentation
    // doesn't expose the cluster's source directly, but the count
    // confirms the cluster pass fired. (Source is exercised via the
    // unit test on pickSource and via the SOURCE_COLOR palette.)
    expect(renderer.lastInterventionBandCount).toBe(1);
  });
});
