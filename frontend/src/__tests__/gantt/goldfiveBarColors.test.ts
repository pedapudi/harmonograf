// Renderer tests for goldfive-translated span bar colors (harmonograf#157).
//
// Confirms that spans carrying the new goldfive.* attributes land in the
// category-specific color bucket — judge on-task / off-task / refine /
// plan / reflective. Exact hex values aren't important (themes can
// override), but the bucket must be distinct from the default CUSTOM
// fill AND distinct per category so operators can tell them apart.

import { beforeEach, describe, expect, it } from 'vitest';
import { GanttRenderer } from '../../gantt/renderer';
import { refreshThemeCache } from '../../gantt/colors';
import { SessionStore } from '../../gantt/index';
import { applyTheme } from '../../theme/themes';
import type { AttributeValue, Span } from '../../gantt/types';

beforeEach(() => {
  // Apply the dark theme so the renderer's cssVar reads resolve to the
  // actual goldfive-* values (each category gets a distinct hex). Without
  // this the :root CSS variables are all unset → cssVar's fallback kicks
  // in and every category collapses onto the same sentinel color.
  applyTheme('dark', 'none');
  refreshThemeCache();
});

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

function attr(value: string): AttributeValue {
  return { kind: 'string', value };
}
function boolAttr(value: boolean): AttributeValue {
  return { kind: 'bool', value };
}

function seedGoldfiveAgent(store: SessionStore): void {
  store.agents.upsert({
    id: 'client-1:goldfive',
    name: 'goldfive',
    framework: 'CUSTOM',
    status: 'CONNECTED',
    capabilities: [],
    connectedAtMs: 1,
    currentActivity: '',
    stuck: false,
    taskReport: '',
    taskReportAt: 0,
    metadata: {},
  });
}

function mkGfSpan(id: string, attrs: Record<string, AttributeValue>): Span {
  return {
    id,
    sessionId: 'sess',
    agentId: 'client-1:goldfive',
    parentSpanId: null,
    kind: 'LLM_CALL',
    name: (attrs['goldfive.call_name'] as { kind: 'string'; value: string } | undefined)?.value ?? 'goldfive',
    status: 'COMPLETED',
    startMs: 1_000,
    endMs: 5_000,
    lane: 0,
    attributes: attrs,
    payloadRefs: [],
    links: [],
    replaced: false,
    error: null,
  };
}

function runPass(store: SessionStore): GanttRenderer {
  const renderer = new GanttRenderer(store);
  renderer.attach(stubCanvas(), stubCanvas(), stubCanvas());
  renderer.resize(1200, 400, 1);
  renderer.setViewport({
    endMs: 10_000,
    windowMs: 10_000,
    liveFollow: false,
    replay: false,
  });
  (renderer as unknown as { drawBlocks: () => void }).drawBlocks();
  return renderer;
}

describe('GanttRenderer — goldfive call-category bar colors', () => {
  it('records distinct fills for judge / refine / plan / reflective categories', () => {
    const store = new SessionStore();
    seedGoldfiveAgent(store);
    store.spans.append(
      mkGfSpan('gf-judge-on', {
        'goldfive.call_name': attr('judge_reasoning'),
        'judge.on_task': boolAttr(true),
      }),
    );
    store.spans.append(
      mkGfSpan('gf-judge-crit', {
        'goldfive.call_name': attr('judge_reasoning'),
        'judge.on_task': boolAttr(false),
        'judge.severity': attr('critical'),
      }),
    );
    store.spans.append(
      mkGfSpan('gf-judge-warn', {
        'goldfive.call_name': attr('judge_reasoning'),
        'judge.on_task': boolAttr(false),
        'judge.severity': attr('warning'),
      }),
    );
    store.spans.append(
      mkGfSpan('gf-refine', {
        'goldfive.call_name': attr('refine_steer'),
      }),
    );
    store.spans.append(
      mkGfSpan('gf-plan', {
        'goldfive.call_name': attr('plan_generate'),
      }),
    );
    store.spans.append(
      mkGfSpan('gf-reflective', {
        'goldfive.call_name': attr('reflective_check'),
      }),
    );

    const renderer = runPass(store);
    const fills = renderer.lastGoldfiveFills;

    // Every category span was recorded.
    expect(fills.size).toBe(6);

    const onTask = fills.get('gf-judge-on');
    const crit = fills.get('gf-judge-crit');
    const warn = fills.get('gf-judge-warn');
    const refine = fills.get('gf-refine');
    const plan = fills.get('gf-plan');
    const reflective = fills.get('gf-reflective');

    // On-task / critical / warning are three distinct judge fills.
    expect(onTask).toBeTruthy();
    expect(crit).toBeTruthy();
    expect(warn).toBeTruthy();
    expect(onTask).not.toBe(crit);
    expect(onTask).not.toBe(warn);
    expect(crit).not.toBe(warn);

    // Refine / plan / reflective each differ from the judge colors and
    // from each other.
    expect(refine).toBeTruthy();
    expect(plan).toBeTruthy();
    expect(reflective).toBeTruthy();
    expect(refine).not.toBe(plan);
    expect(refine).not.toBe(onTask);
    expect(plan).not.toBe(reflective);

    renderer.detach();
  });

  it('does not record a fill override for non-goldfive spans (graceful degradation)', () => {
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
    const s: Span = {
      id: 'sp-plain',
      sessionId: 'sess',
      agentId: 'agent-a',
      parentSpanId: null,
      kind: 'LLM_CALL',
      name: 'gemini-2',
      status: 'COMPLETED',
      startMs: 1_000,
      endMs: 5_000,
      lane: 0,
      attributes: {},
      payloadRefs: [],
      links: [],
      replaced: false,
      error: null,
    };
    store.spans.append(s);
    const renderer = runPass(store);
    expect(renderer.lastGoldfiveFills.size).toBe(0);
    renderer.detach();
  });

  it('leaves FAILED goldfive spans with the error fill (status wins over category)', () => {
    const store = new SessionStore();
    seedGoldfiveAgent(store);
    const s = mkGfSpan('gf-refine-failed', {
      'goldfive.call_name': attr('refine_steer'),
    });
    s.status = 'FAILED';
    store.spans.append(s);
    const renderer = runPass(store);
    // Fill override skipped on FAILED so tests of error rendering
    // continue to work and the red "broken" fill isn't masked by the
    // refine purple.
    expect(renderer.lastGoldfiveFills.has('gf-refine-failed')).toBe(false);
    renderer.detach();
  });

  it('does not override an unknown call_name (falls through to default CUSTOM fill)', () => {
    const store = new SessionStore();
    seedGoldfiveAgent(store);
    store.spans.append(
      mkGfSpan('gf-unknown', {
        'goldfive.call_name': attr('some_future_call_name'),
      }),
    );
    const renderer = runPass(store);
    expect(renderer.lastGoldfiveFills.has('gf-unknown')).toBe(false);
    renderer.detach();
  });
});
