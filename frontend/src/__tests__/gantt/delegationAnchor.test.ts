// Regression for the off-center delegation arrow bug: the endpoints must
// anchor to the INVOCATION bar's Y (sub-lane 0 center), NOT the row's
// vertical midline. On a ROW_HEIGHT_PX=56 row the bar center sits ~18px
// above the row midline, and the user observed the arrow floating that
// gap away from the bar on the live path. Refresh happened to mask the
// same math because the burst orders spans ahead of delegations, but
// both paths should land on the bar now.
//
// We drive drawBlocks (private) through the typed escape hatch used by
// brainBadges.test.ts. The renderer needs a canvas 2D context; jsdom
// doesn't ship one, so we stub it with a no-op Proxy and inspect the
// cached DelegationEdgeLayout the draw pass populates.

import { describe, expect, it } from 'vitest';
import { GanttRenderer } from '../../gantt/renderer';
import { SessionStore } from '../../gantt/index';
import {
  GUTTER_WIDTH_PX,
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
    kind: 'INVOCATION',
    name: 'run',
    status: 'RUNNING',
    startMs: 0,
    endMs: null,
    lane: 0,
    attributes: {},
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
    ...overrides,
  };
}

// Re-derive the same lane-0 center the renderer uses. Keeping this in the
// test (rather than importing the renderer's private helper) documents the
// contract: the INVOCATION bar's Y is a function of row.top + lane math,
// NOT row midline.
function expectedInvocationCenter(rowTop: number, rowHeight: number): number {
  const laneH = Math.max(SUB_LANE_HEIGHT_PX, Math.floor(rowHeight / 3));
  const laneTop = rowTop + 2;
  const laneBot = Math.min(rowTop + rowHeight - 2, laneTop + laneH - 2);
  const rectH = Math.max(6, laneBot - laneTop);
  return laneTop + rectH / 2;
}

function drawBlocks(renderer: GanttRenderer): void {
  (renderer as unknown as { drawBlocks: () => void }).drawBlocks();
}

describe('GanttRenderer delegation anchor (harmonograf#129)', () => {
  it('anchors endpoints to the INVOCATION bar center, not the row midline', () => {
    const store = new SessionStore();
    mkAgent(store, 'coord');
    mkAgent(store, 'sub');

    // Coordinator INVOCATION is wide, sub-agent INVOCATION starts at the
    // delegation moment and runs past it. Both sit at sub-lane 0.
    store.spans.append(
      mkSpan({
        id: 'inv-coord',
        agentId: 'coord',
        startMs: 0,
        endMs: 10_000,
        status: 'COMPLETED',
      }),
    );
    store.spans.append(
      mkSpan({
        id: 'inv-sub',
        agentId: 'sub',
        startMs: 1_000,
        endMs: 8_000,
        status: 'COMPLETED',
      }),
    );

    store.delegations.append({
      fromAgentId: 'coord',
      toAgentId: 'sub',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 1_000,
    });

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);
    r.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });
    drawBlocks(r);

    const edges = r._delegationLayoutsForTesting();
    expect(edges).toHaveLength(1);
    const e = edges[0];

    // Rows are laid out top-down: coord first, sub second.
    const coordTop = TOP_MARGIN_PX;
    const subTop = TOP_MARGIN_PX + ROW_HEIGHT_PX;

    const coordBarY = expectedInvocationCenter(coordTop, ROW_HEIGHT_PX);
    const subBarY = expectedInvocationCenter(subTop, ROW_HEIGHT_PX);
    const coordRowMid = coordTop + ROW_HEIGHT_PX / 2;
    const subRowMid = subTop + ROW_HEIGHT_PX / 2;

    // The old (buggy) behavior anchored at row midline. Assert we moved
    // off it — and specifically landed on the bar.
    expect(e.srcY).toBe(coordBarY);
    expect(e.tgtY).toBe(subBarY);
    expect(e.srcY).not.toBe(coordRowMid);
    expect(e.tgtY).not.toBe(subRowMid);

    r.detach();
  });

  it('lane-0 fallback holds when the target INVOCATION has not arrived yet (live path)', () => {
    // Reproduces the race the screenshot captured: coordinator's
    // INVOCATION is live, delegation observed, but the sub-agent's
    // INVOCATION span hasn't been reported yet. The arrow must still
    // land where the bar *will* be drawn once it arrives (sub-lane 0),
    // not at the row midline.
    const store = new SessionStore();
    mkAgent(store, 'coord');
    mkAgent(store, 'sub');

    store.spans.append(
      mkSpan({
        id: 'inv-coord',
        agentId: 'coord',
        startMs: 0,
        endMs: null,
        status: 'RUNNING',
      }),
    );

    store.delegations.append({
      fromAgentId: 'coord',
      toAgentId: 'sub',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 500,
    });

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);
    r.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });
    drawBlocks(r);

    const edges = r._delegationLayoutsForTesting();
    expect(edges).toHaveLength(1);
    const e = edges[0];

    const subTop = TOP_MARGIN_PX + ROW_HEIGHT_PX;
    const subBarY = expectedInvocationCenter(subTop, ROW_HEIGHT_PX);
    const subRowMid = subTop + ROW_HEIGHT_PX / 2;

    expect(e.tgtY).toBe(subBarY);
    expect(e.tgtY).not.toBe(subRowMid);

    r.detach();
  });

  it('anchors correctly on a row with multiple active sub-lanes', () => {
    // Sub-agent has an INVOCATION at lane 0 plus overlapping LLM_CALL /
    // TOOL_CALL spans packed onto lane 1 and lane 2 — the bar Y is
    // still lane-0 center, regardless of how many concurrent children
    // are stacked alongside it.
    const store = new SessionStore();
    mkAgent(store, 'coord');
    mkAgent(store, 'sub');

    store.spans.append(
      mkSpan({
        id: 'inv-coord',
        agentId: 'coord',
        startMs: 0,
        endMs: 10_000,
        status: 'COMPLETED',
      }),
    );
    store.spans.append(
      mkSpan({
        id: 'inv-sub',
        agentId: 'sub',
        startMs: 1_000,
        endMs: 8_000,
        status: 'COMPLETED',
      }),
    );
    // Two concurrent children. packLanes() would put them on lane 1 and
    // lane 2. We pre-assign here because the store doesn't auto-pack.
    store.spans.append(
      mkSpan({
        id: 'llm-1',
        agentId: 'sub',
        kind: 'LLM_CALL',
        startMs: 1_500,
        endMs: 3_500,
        lane: 1,
        status: 'COMPLETED',
      }),
    );
    store.spans.append(
      mkSpan({
        id: 'tool-1',
        agentId: 'sub',
        kind: 'TOOL_CALL',
        startMs: 1_500,
        endMs: 3_500,
        lane: 2,
        status: 'COMPLETED',
      }),
    );

    store.delegations.append({
      fromAgentId: 'coord',
      toAgentId: 'sub',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 1_000,
    });

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);
    r.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });
    drawBlocks(r);

    const edges = r._delegationLayoutsForTesting();
    expect(edges).toHaveLength(1);
    const e = edges[0];

    const subTop = TOP_MARGIN_PX + ROW_HEIGHT_PX;
    const subBarY = expectedInvocationCenter(subTop, ROW_HEIGHT_PX);

    expect(e.tgtY).toBe(subBarY);

    r.detach();
  });
});

// harmonograf#241: delegation arrow TAIL must anchor at the source span's
// END time (where the hand-off actually happened on the coordinator's
// bar), NOT at the observed-time of the delegation_observed event. The
// previous behavior set both endpoints to observedAtMs which collapsed
// the tail onto a single x — for delegations dispatched near session
// start that put the tail right at the leftmost data position. The
// arrowhead (target side) is unchanged.
describe('GanttRenderer delegation tail anchoring (harmonograf#241)', () => {
  function pxAt(ms: number, windowMs = 10_000, widthCss = 1200): number {
    // Mirror msToPx(): viewport.startMs is endMs - windowMs.
    const startMs = 10_000 - windowMs;
    const dataW = widthCss - GUTTER_WIDTH_PX;
    return GUTTER_WIDTH_PX + ((ms - startMs) / windowMs) * dataW;
  }

  it('tail anchors at the source INVOCATION end when the source has completed before observation', () => {
    // Scenario: coordinator runs 0→500ms, completes; sub starts at 700ms.
    // Delegation observed at 700ms (sub start). Tail x must land at the
    // coordinator's endMs (500), NOT at observedAtMs (700) — though in
    // this fixture observedAtMs===subStart so head/tail differ visibly.
    const store = new SessionStore();
    mkAgent(store, 'coord');
    mkAgent(store, 'sub');

    store.spans.append(
      mkSpan({
        id: 'inv-coord',
        agentId: 'coord',
        startMs: 0,
        endMs: 500,
        status: 'COMPLETED',
      }),
    );
    store.spans.append(
      mkSpan({
        id: 'inv-sub',
        agentId: 'sub',
        startMs: 700,
        endMs: 4_000,
        status: 'COMPLETED',
      }),
    );

    store.delegations.append({
      fromAgentId: 'coord',
      toAgentId: 'sub',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 700,
    });

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);
    r.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });
    drawBlocks(r);

    const edges = r._delegationLayoutsForTesting();
    expect(edges).toHaveLength(1);
    const e = edges[0];

    // Head (tgtX) lands at observedAtMs=700; tail (srcX) lands at coord
    // endMs=500. The two must differ — that's the whole point of the fix.
    expect(e.tgtX).toBeCloseTo(pxAt(700), 5);
    expect(e.srcX).toBeCloseTo(pxAt(500), 5);
    expect(e.srcX).toBeLessThan(e.tgtX);
  });

  it('tail uses observed time when source span endMs is AFTER observation (clock drift safeguard)', () => {
    // Scenario: coordinator INVOCATION end stamp is past the
    // delegation_observed event (an agent's own bar can outlive the
    // hand-off when it spawns then continues to do other things). Tail
    // must NOT precede head — fall back to observedAtMs.
    const store = new SessionStore();
    mkAgent(store, 'coord');
    mkAgent(store, 'sub');

    store.spans.append(
      mkSpan({
        id: 'inv-coord',
        agentId: 'coord',
        startMs: 0,
        endMs: 5_000, // past the delegation
        status: 'COMPLETED',
      }),
    );
    store.spans.append(
      mkSpan({
        id: 'inv-sub',
        agentId: 'sub',
        startMs: 1_000,
        endMs: 4_000,
        status: 'COMPLETED',
      }),
    );

    store.delegations.append({
      fromAgentId: 'coord',
      toAgentId: 'sub',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 1_000,
    });

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);
    r.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });
    drawBlocks(r);

    const edges = r._delegationLayoutsForTesting();
    expect(edges).toHaveLength(1);
    const e = edges[0];

    // Degenerate fallback: srcX === tgtX (both at observedAtMs).
    expect(e.srcX).toBe(e.tgtX);
    expect(e.tgtX).toBeCloseTo(pxAt(1_000), 5);
  });

  it('tail anchors at store.nowMs when the source span is still RUNNING', () => {
    // Scenario: coordinator is still RUNNING when the delegation is
    // observed. Tail tracks the session's "now" cursor so the live edge
    // of the bar holds the arrow's tail — same visual contract as the
    // bar itself, which the renderer paints out to nowMs while running.
    const store = new SessionStore();
    mkAgent(store, 'coord');
    mkAgent(store, 'sub');

    store.spans.append(
      mkSpan({
        id: 'inv-coord',
        agentId: 'coord',
        startMs: 0,
        endMs: null,
        status: 'RUNNING',
      }),
    );
    store.spans.append(
      mkSpan({
        id: 'inv-sub',
        agentId: 'sub',
        startMs: 2_000,
        endMs: null,
        status: 'RUNNING',
      }),
    );

    store.delegations.append({
      fromAgentId: 'coord',
      toAgentId: 'sub',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 2_000,
    });
    // Session has progressed past the delegation moment.
    store.nowMs = 1_500;

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);
    r.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });
    drawBlocks(r);

    const edges = r._delegationLayoutsForTesting();
    expect(edges).toHaveLength(1);
    const e = edges[0];

    // Tail at nowMs (1500), head at observedAtMs (2000).
    expect(e.srcX).toBeCloseTo(pxAt(1_500), 5);
    expect(e.tgtX).toBeCloseTo(pxAt(2_000), 5);
    expect(e.srcX).toBeLessThan(e.tgtX);
  });

  it('falls back to observedAtMs when no source INVOCATION span has been reported yet', () => {
    // Scenario: source row has no INVOCATION span yet (delegation
    // ingested before the coordinator's span landed). Tail degenerates
    // to observedAtMs so the arrow stays visible.
    const store = new SessionStore();
    mkAgent(store, 'coord');
    mkAgent(store, 'sub');

    store.delegations.append({
      fromAgentId: 'coord',
      toAgentId: 'sub',
      taskId: 't-1',
      invocationId: 'inv-1',
      observedAtMs: 1_500,
    });

    const r = new GanttRenderer(store);
    r.attach(stubCanvas(), stubCanvas(), stubCanvas());
    r.resize(1200, 200, 1);
    r.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });
    drawBlocks(r);

    const edges = r._delegationLayoutsForTesting();
    expect(edges).toHaveLength(1);
    const e = edges[0];

    expect(e.srcX).toBe(e.tgtX);
    expect(e.tgtX).toBeCloseTo(pxAt(1_500), 5);
  });
});
