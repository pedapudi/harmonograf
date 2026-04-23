// Regression for the brain-badge-not-rendering bug: the Gantt renderer
// must re-evaluate hasThinking(span) every frame so that when a reasoning
// attribute arrives via stream (as an UpdatedSpan or the SPAN_UPDATE we
// now emit just before SPAN_END), the next drawBlocks pass picks it up
// and renders the 🧠 badge. See harmonograf#[this PR].
//
// The renderer requires a 2D canvas context; jsdom doesn't provide one,
// so we stub getContext with a Proxy that no-ops every method. The test
// only inspects `renderer.lastBrainBadgeCount`, which drawBlocks writes
// at the end of the pass — pixel output doesn't matter.

import { describe, expect, it } from 'vitest';
import { GanttRenderer } from '../../gantt/renderer';
import { SessionStore } from '../../gantt/index';
import type { Span } from '../../gantt/types';

function stubCtx(): CanvasRenderingContext2D {
  // A Proxy that swallows every property access. Methods return the
  // proxy itself so chained calls like ctx.save().beginPath() still work,
  // and property reads return sensible defaults where the renderer
  // actually reads back.
  const handler: ProxyHandler<object> = {
    get(_t, prop) {
      if (prop === 'canvas') return { width: 1200, height: 400 };
      if (prop === 'globalAlpha') return 1;
      if (prop === 'measureText') return () => ({ width: 10 });
      // Everything else — fillRect, clearRect, save, restore, beginPath,
      // clip, setLineDash, etc. — is a no-op function.
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
  // getContext returns null in jsdom; swap in the stub.
  (el as unknown as { getContext: () => CanvasRenderingContext2D }).getContext =
    () => stubCtx();
  return el;
}

function mkLlmSpan(overrides: Partial<Span> = {}): Span {
  return {
    id: 'sp-llm',
    sessionId: 'sess',
    agentId: 'agent-a',
    parentSpanId: null,
    kind: 'LLM_CALL',
    name: 'gemini-2.0',
    status: 'RUNNING',
    startMs: 1_000,
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

describe('GanttRenderer brain badges — stream update reactivity', () => {
  it('renders a brain badge when has_reasoning arrives on an already-placed LLM_CALL', () => {
    const store = new SessionStore();
    // Stamp an agent so queryAgent has a row to walk. Name + framework are
    // unused by drawBlocks beyond painting labels (which the ctx stub
    // swallows), so minimal shape is fine.
    store.agents.upsert({
      id: 'agent-a',
      name: 'agent-a',
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
    // Span starts WITHOUT reasoning attributes — the common case where
    // the client hasn't yet run after_model_callback.
    const span = mkLlmSpan({ startMs: 1_000, endMs: 5_000, status: 'COMPLETED' });
    store.spans.append(span);

    const renderer = new GanttRenderer(store);
    const bg = stubCanvas();
    const blocks = stubCanvas();
    const overlay = stubCanvas();
    renderer.attach(bg, blocks, overlay);
    renderer.resize(1200, 200, 1);

    // Position the viewport so the span sits comfortably inside and is
    // rendered wide enough to clear the width>=14 gate (the badge is
    // skipped on narrow bars to avoid colliding with the kind icon).
    renderer.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });

    // First frame: no reasoning attributes → no badge.
    // drawBlocks is private; drive it via the public frame path by
    // triggering a dirty + calling the internal frame handler. Because
    // rAF is async in jsdom, we sidestep it by calling the private
    // drawBlocks directly through the typed escape hatch — this is the
    // same approach existing tests use for _seedDelegationLayoutsForTesting.
    (renderer as unknown as { drawBlocks: () => void }).drawBlocks();
    expect(renderer.lastBrainBadgeCount).toBe(0);

    // Now simulate the stream update: the server's SPAN_UPDATE delta
    // arrives with has_reasoning=true + llm.reasoning. The rpc hook
    // mutates the existing span's attributes in place, then calls
    // store.spans.update — mirroring that here.
    const existing = store.spans.get('sp-llm')!;
    existing.attributes['has_reasoning'] = { kind: 'bool', value: true };
    existing.attributes['llm.reasoning'] = {
      kind: 'string',
      value: 'i thought carefully',
    };
    store.spans.update(existing);

    // Next frame: renderer must re-scan hasThinking and emit the badge.
    (renderer as unknown as { drawBlocks: () => void }).drawBlocks();
    expect(renderer.lastBrainBadgeCount).toBe(1);

    renderer.detach();
  });

  it('renders a brain badge when only llm.reasoning_trail arrives (no has_reasoning flag)', () => {
    // Covers the INVOCATION-span path where the plugin stamps the
    // aggregated trail but hasThinking() falls back to attribute text
    // when the has_reasoning bool is missing.
    const store = new SessionStore();
    store.agents.upsert({
      id: 'agent-a',
      name: 'agent-a',
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
    const span = mkLlmSpan({
      id: 'sp-llm-2',
      startMs: 1_000,
      endMs: 5_000,
      status: 'COMPLETED',
    });
    store.spans.append(span);

    const renderer = new GanttRenderer(store);
    renderer.attach(stubCanvas(), stubCanvas(), stubCanvas());
    renderer.resize(1200, 200, 1);
    renderer.setViewport({
      endMs: 10_000,
      windowMs: 10_000,
      liveFollow: false,
      replay: false,
    });

    (renderer as unknown as { drawBlocks: () => void }).drawBlocks();
    expect(renderer.lastBrainBadgeCount).toBe(0);

    const existing = store.spans.get('sp-llm-2')!;
    existing.attributes['llm.reasoning_trail'] = {
      kind: 'string',
      value: '[LLM call 1] thought',
    };
    store.spans.update(existing);

    (renderer as unknown as { drawBlocks: () => void }).drawBlocks();
    expect(renderer.lastBrainBadgeCount).toBe(1);

    renderer.detach();
  });
});
