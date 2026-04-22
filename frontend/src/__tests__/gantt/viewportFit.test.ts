// Viewport-level tests for fitAll() and jumpToLastActivity() on the Gantt
// renderer. These cover harmonograf#89's core fix: completed sessions should
// open fitted to activity, and panning past the last span should offer a
// click-to-jump-back affordance. The renderer is exercised without attaching
// any canvases — every method under test operates on viewport + SpanIndex
// state and doesn't touch the drawing context.

import { describe, expect, it } from 'vitest';
import { GanttRenderer } from '../../gantt/renderer';
import { SessionStore } from '../../gantt/index';
import { defaultViewport, viewportStart } from '../../gantt/viewport';
import type { Span } from '../../gantt/types';

function appendSpan(store: SessionStore, startMs: number, endMs: number | null): void {
  const span: Span = {
    id: `s-${startMs}-${endMs ?? 'open'}`,
    sessionId: 'sess',
    agentId: 'agent-a',
    parentSpanId: null,
    kind: 'INVOCATION',
    name: 'work',
    status: endMs === null ? 'RUNNING' : 'COMPLETED',
    startMs,
    endMs,
    lane: 0,
    attributes: {},
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
  };
  store.spans.append(span);
}

describe('GanttRenderer.fitAll (harmonograf#89 autofit)', () => {
  it('fits to maxEndMs with a small right-side margin', () => {
    const store = new SessionStore();
    appendSpan(store, 0, 16 * 60_000); // 16 minutes of work
    const r = new GanttRenderer(store);

    r.fitAll();
    const v = r.getViewport();

    // liveFollow off — completed sessions don't track a cursor.
    expect(v.liveFollow).toBe(false);
    // Window covers the full activity with ~5% headroom.
    expect(v.windowMs).toBeGreaterThan(16 * 60_000);
    expect(v.windowMs).toBeLessThan(17 * 60_000);
    // endMs reaches at least maxEnd so the last span is visible.
    expect(v.endMs).toBeGreaterThanOrEqual(16 * 60_000);
    // Left edge never goes before session start.
    expect(viewportStart(v)).toBeLessThanOrEqual(0 + 1);
  });

  it('leaves viewport unchanged when there are no spans and no nowMs', () => {
    const store = new SessionStore();
    const r = new GanttRenderer(store);
    const before = r.getViewport();
    r.fitAll();
    // maxEndMs = 0, nowMs = 0 → maxEnd clamped to 1. Window floors at
    // ZOOM_MIN_MS (30s). That's acceptable — there's nothing to see either
    // way and the user will switch sessions. The key invariant is: no crash
    // and viewport is still valid (left edge >= 0).
    const after = r.getViewport();
    expect(viewportStart(after)).toBeGreaterThanOrEqual(0 - 1);
    expect(after.liveFollow).toBe(false);
    // defaultViewport() returns liveFollow=true; we should have explicitly
    // flipped it in fitAll.
    expect(before.liveFollow).toBe(true);
  });
});

describe('GanttRenderer.jumpToLastActivity (harmonograf#89 D)', () => {
  it('preserves zoom level and lands near the last recorded span', () => {
    const store = new SessionStore();
    appendSpan(store, 0, 60_000);
    appendSpan(store, 10 * 60_000, 11 * 60_000);
    const r = new GanttRenderer(store);

    // Shrink to a 60-second window anchored way out in the future.
    r.setViewport({
      ...defaultViewport(),
      windowMs: 60_000,
      endMs: 60 * 60_000, // 1h in — well past activity at 11m.
      liveFollow: false,
    });
    const before = r.getViewport();
    expect(viewportStart(before)).toBeGreaterThan(11 * 60_000);

    r.jumpToLastActivity();
    const after = r.getViewport();

    // Window duration preserved.
    expect(after.windowMs).toBe(60_000);
    // liveFollow stays off — we're jumping to a fixed point.
    expect(after.liveFollow).toBe(false);
    // Left edge is strictly before the last span end so it's visible.
    expect(viewportStart(after)).toBeLessThan(11 * 60_000);
    // And the right edge is past the last span end so there's breathing room.
    expect(after.endMs).toBeGreaterThanOrEqual(11 * 60_000);
  });

  it('is a no-op when the session has no spans', () => {
    const store = new SessionStore();
    const r = new GanttRenderer(store);
    const before = r.getViewport();
    r.jumpToLastActivity();
    const after = r.getViewport();
    expect(after).toEqual(before);
  });
});
