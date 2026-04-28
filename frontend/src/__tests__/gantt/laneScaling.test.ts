// Regression for harmonograf#271: an agent with multiple concurrent spans
// must vertically scale its row so each span lands on a distinct sub-track,
// instead of every span stacking on lane 0 and the rear ones disappearing
// behind the front (the v15presmtx-1 goldfive lane symptom).
//
// We exercise three contracts:
//   1. packLanes() returns the correct lane count and assigns concurrent
//      spans to distinct lane indices (max-concurrency = N → uses N lanes).
//   2. GanttRenderer.rowHeight scales above ROW_HEIGHT_PX once the agent's
//      lane count exceeds the legacy 3-track budget.
//   3. The rectangles for two concurrent spans on the same agent end up at
//      different y offsets (no overlap on the canvas).

import { describe, expect, it } from 'vitest';
import { GanttRenderer } from '../../gantt/renderer';
import { SessionStore } from '../../gantt/index';
import { packLanes } from '../../gantt/layout';
import {
  ROW_HEIGHT_PX,
  SUB_LANE_HEIGHT_PX,
  TOP_MARGIN_PX,
} from '../../gantt/viewport';
import type { Span } from '../../gantt/types';

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

function mkAgent(store: SessionStore, id: string): void {
  store.agents.upsert({
    id,
    name: id,
    framework: 'ADK',
    status: 'CONNECTED',
    capabilities: [],
    connectedAtMs: 0,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
  });
}

function mkSpan(overrides: Partial<Span> & { id: string; agentId: string }): Span {
  return {
    sessionId: 'sess',
    parentSpanId: null,
    kind: 'CUSTOM',
    name: 'span',
    status: 'COMPLETED',
    startMs: 0,
    endMs: null,
    lane: -1,
    attributes: {},
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
    ...overrides,
  };
}

describe('packLanes() concurrency tracking (harmonograf#271)', () => {
  it('assigns two concurrent spans to distinct lanes and reports laneCount=2', () => {
    const spans: Span[] = [
      mkSpan({ id: 'a', agentId: 'g', startMs: 0, endMs: 1000 }),
      mkSpan({ id: 'b', agentId: 'g', startMs: 200, endMs: 800 }),
    ];
    const count = packLanes(spans);
    expect(count).toBe(2);
    const a = spans.find((s) => s.id === 'a')!;
    const b = spans.find((s) => s.id === 'b')!;
    expect(a.lane).not.toBe(b.lane);
    expect(new Set([a.lane, b.lane])).toEqual(new Set([0, 1]));
  });

  it('reuses lane 0 once the prior span ends (no over-allocation)', () => {
    const spans: Span[] = [
      mkSpan({ id: 'a', agentId: 'g', startMs: 0, endMs: 100 }),
      mkSpan({ id: 'b', agentId: 'g', startMs: 200, endMs: 300 }),
    ];
    const count = packLanes(spans);
    expect(count).toBe(1);
    expect(spans[0].lane).toBe(0);
    expect(spans[1].lane).toBe(0);
  });

  it('handles 4-way concurrency (lane budget exceeds default 3-track row)', () => {
    const spans: Span[] = [
      mkSpan({ id: 'a', agentId: 'g', startMs: 0, endMs: 1000 }),
      mkSpan({ id: 'b', agentId: 'g', startMs: 100, endMs: 900 }),
      mkSpan({ id: 'c', agentId: 'g', startMs: 200, endMs: 800 }),
      mkSpan({ id: 'd', agentId: 'g', startMs: 300, endMs: 700 }),
    ];
    const count = packLanes(spans);
    expect(count).toBe(4);
    const lanes = spans.map((s) => s.lane).sort();
    expect(lanes).toEqual([0, 1, 2, 3]);
  });
});

describe('GanttRenderer row-height scaling (harmonograf#271)', () => {
  it('keeps the default row height for <=3 concurrent spans', () => {
    const store = new SessionStore();
    mkAgent(store, 'g');
    store.spans.append(
      mkSpan({ id: 'a', agentId: 'g', startMs: 0, endMs: 1000 }),
    );
    store.spans.append(
      mkSpan({ id: 'b', agentId: 'g', startMs: 200, endMs: 800 }),
    );
    packLanes(
      store.spans.queryAgent(
        'g',
        -Number.MAX_SAFE_INTEGER,
        Number.MAX_SAFE_INTEGER,
      ),
    );

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);

    const layout = r.getRowLayout();
    expect(layout).toHaveLength(1);
    expect(layout[0].height).toBe(ROW_HEIGHT_PX);
    r.detach();
  });

  it('scales the row vertically when concurrency exceeds the 3-track budget', () => {
    const store = new SessionStore();
    mkAgent(store, 'g');
    // Four mutually-overlapping spans → laneCount=4.
    for (const id of ['a', 'b', 'c', 'd']) {
      store.spans.append(
        mkSpan({
          id,
          agentId: 'g',
          startMs: 0 + ['a', 'b', 'c', 'd'].indexOf(id) * 50,
          endMs: 10_000,
        }),
      );
    }
    packLanes(
      store.spans.queryAgent(
        'g',
        -Number.MAX_SAFE_INTEGER,
        Number.MAX_SAFE_INTEGER,
      ),
    );

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 400, 1);

    const layout = r.getRowLayout();
    expect(layout).toHaveLength(1);
    // The 3-lane base height was ROW_HEIGHT_PX = 56 → baseLaneH=18; for
    // laneCount=4 the row must be at least 4*18 + 4 = 76px.
    const baseLaneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(ROW_HEIGHT_PX / 3));
    expect(layout[0].height).toBeGreaterThan(ROW_HEIGHT_PX);
    expect(layout[0].height).toBe(4 * baseLaneH + 4);
    r.detach();
  });

  it('places two concurrent spans at distinct y offsets via rectFor()', () => {
    const store = new SessionStore();
    mkAgent(store, 'g');
    store.spans.append(
      mkSpan({ id: 'front', agentId: 'g', startMs: 0, endMs: 5_000 }),
    );
    store.spans.append(
      mkSpan({ id: 'rear', agentId: 'g', startMs: 1_000, endMs: 4_000 }),
    );
    packLanes(
      store.spans.queryAgent(
        'g',
        -Number.MAX_SAFE_INTEGER,
        Number.MAX_SAFE_INTEGER,
      ),
    );

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);
    r.setViewport({
      endMs: 5_000,
      windowMs: 5_000,
      liveFollow: false,
      replay: false,
    });

    const front = r.rectFor('front');
    const rear = r.rectFor('rear');
    expect(front).not.toBeNull();
    expect(rear).not.toBeNull();
    expect(front!.y).not.toBe(rear!.y);
    // Both rects should fit inside the row's vertical extent.
    const layout = r.getRowLayout();
    const top = layout[0].top;
    const bottom = top + layout[0].height;
    expect(front!.y).toBeGreaterThanOrEqual(top);
    expect(front!.y + front!.h).toBeLessThanOrEqual(bottom);
    expect(rear!.y).toBeGreaterThanOrEqual(top);
    expect(rear!.y + rear!.h).toBeLessThanOrEqual(bottom);
    // Sanity: row above the agent is the time axis margin.
    expect(top).toBe(TOP_MARGIN_PX);
    r.detach();
  });
});
