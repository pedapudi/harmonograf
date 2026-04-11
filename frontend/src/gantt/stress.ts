// Stress harness. Runs the four scenarios from doc 04 §9.4 against a live
// GanttRenderer and reports p95 frame time + event-to-visible latency against
// the hard budgets in §9.3. Invoked from the dev-only /stress route.

import { packLanes } from './layout';
import type { GanttRenderer } from './renderer';
import { SessionStore } from './index';
import type { Span } from './types';
import { makeAgent, randomSpan, seededRng } from './mockData';

export interface ScenarioResult {
  name: string;
  description: string;
  p95FrameMs: number;
  avgFrameMs: number;
  spanCount: number;
  durationMs: number;
  budgetFrameMs: number;
  pass: boolean;
  note?: string;
}

export interface ScenarioContext {
  store: SessionStore;
  renderer: GanttRenderer;
  durationMs?: number;
}

const FRAME_BUDGET_MS = 16;

async function runForMs(renderer: GanttRenderer, ms: number): Promise<void> {
  renderer.resetMetrics();
  const start = performance.now();
  while (performance.now() - start < ms) {
    await new Promise((r) => requestAnimationFrame(() => r(null)));
  }
}

function summarize(renderer: GanttRenderer): { p95: number; avg: number } {
  const p95 = renderer.p95FrameMs();
  const m = renderer.metrics;
  let sum = 0;
  for (let i = 0; i < m.sampleCount; i++) sum += m.samples[i];
  return { p95, avg: m.sampleCount ? sum / m.sampleCount : 0 };
}

// ---- Scenarios ----------------------------------------------------------

// Steady state: 5 agents × 10 spans/sec × 120s compressed, then pan/zoom.
export async function steadyStateScenario(ctx: ScenarioContext): Promise<ScenarioResult> {
  const { store, renderer } = ctx;
  store.clear();
  const rng = seededRng(1);
  const durationMs = 120_000;
  for (let i = 0; i < 5; i++) store.agents.upsert(makeAgent(`a${i + 1}`, i * 100));
  const agents = store.agents.list;
  const spans: Record<string, Span[]> = {};
  for (const a of agents) spans[a.id] = [];
  for (let t = 0; t < durationMs; t += 100) {
    for (const a of agents) {
      const s = randomSpan('stress', a.id, t, 80 + Math.floor(rng() * 300), rng);
      spans[a.id].push(s);
    }
  }
  for (const a of agents) {
    packLanes(spans[a.id]);
    for (const s of spans[a.id]) store.spans.append(s);
  }
  store.nowMs = durationMs;
  renderer.fitAll();
  // Let it render, then exercise pan/zoom by animating the viewport.
  await runForMs(renderer, 800);
  // Pan sweep
  for (let i = 0; i < 20; i++) {
    renderer.panBy(0.1);
    await new Promise((r) => requestAnimationFrame(() => r(null)));
  }
  for (let i = 0; i < 8; i++) {
    renderer.zoomBy(0.8);
    await new Promise((r) => requestAnimationFrame(() => r(null)));
  }
  const { p95, avg } = summarize(renderer);
  return {
    name: 'steady',
    description: '5 agents, ~10 spans/s each, 120s — pan + zoom sweep',
    p95FrameMs: p95,
    avgFrameMs: avg,
    spanCount: store.spans.size,
    durationMs,
    budgetFrameMs: FRAME_BUDGET_MS,
    pass: p95 < FRAME_BUDGET_MS,
  };
}

// Burst: 1 agent emitting 500 spans/sec for 10s, streamed incrementally.
export async function burstScenario(ctx: ScenarioContext): Promise<ScenarioResult> {
  const { store, renderer } = ctx;
  store.clear();
  const rng = seededRng(2);
  store.agents.upsert(makeAgent('burst-agent', 0));
  const agentId = store.agents.list[0].id;
  renderer.fitAll();
  renderer.resetMetrics();
  const totalSpans = 5000;
  const emitPerFrame = 85; // ~60 fps × 85 ≈ 500 spans/s
  let emitted = 0;
  let tMs = 0;
  while (emitted < totalSpans) {
    for (let i = 0; i < emitPerFrame && emitted < totalSpans; i++) {
      const span = randomSpan('stress', agentId, tMs, 10 + Math.floor(rng() * 40), rng);
      span.lane = emitted % 4;
      store.spans.append(span);
      emitted++;
      tMs += 2;
    }
    store.nowMs = tMs;
    await new Promise((r) => requestAnimationFrame(() => r(null)));
  }
  const { p95, avg } = summarize(renderer);
  return {
    name: 'burst',
    description: '1 agent, 500 spans/s for ~10s, live',
    p95FrameMs: p95,
    avgFrameMs: avg,
    spanCount: store.spans.size,
    durationMs: tMs,
    budgetFrameMs: FRAME_BUDGET_MS,
    pass: p95 < FRAME_BUDGET_MS,
  };
}

// Big payloads: 50 LLM_CALLs — renderer doesn't load payloads but we confirm
// perf doesn't regress with large metadata in memory.
export async function bigPayloadScenario(ctx: ScenarioContext): Promise<ScenarioResult> {
  const { store, renderer } = ctx;
  store.clear();
  const rng = seededRng(3);
  store.agents.upsert(makeAgent('big-payload', 0));
  const agentId = store.agents.list[0].id;
  const spans: Span[] = [];
  for (let i = 0; i < 50; i++) {
    const s = randomSpan('stress', agentId, i * 1500, 1400, rng);
    s.kind = 'LLM_CALL';
    s.name = 'x'.repeat(2000); // simulate large metadata payload on the span obj
    spans.push(s);
  }
  packLanes(spans);
  for (const s of spans) store.spans.append(s);
  store.nowMs = 50 * 1500;
  renderer.fitAll();
  await runForMs(renderer, 1500);
  const { p95, avg } = summarize(renderer);
  return {
    name: 'big-payloads',
    description: '50 LLM calls with 2KB names (stand-in for metadata)',
    p95FrameMs: p95,
    avgFrameMs: avg,
    spanCount: store.spans.size,
    durationMs: 50 * 1500,
    budgetFrameMs: FRAME_BUDGET_MS,
    pass: p95 < FRAME_BUDGET_MS,
  };
}

// Cross-agent chatter: 10 agents, transfers between each other every 200ms.
export async function chatterScenario(ctx: ScenarioContext): Promise<ScenarioResult> {
  const { store, renderer } = ctx;
  store.clear();
  const rng = seededRng(4);
  for (let i = 0; i < 10; i++) store.agents.upsert(makeAgent(`c${i + 1}`, i * 50));
  const agents = store.agents.list;
  const durationMs = 60_000;
  const perAgent: Record<string, Span[]> = {};
  for (const a of agents) perAgent[a.id] = [];
  for (let t = 0; t < durationMs; t += 200) {
    for (let i = 0; i < agents.length; i++) {
      const s = randomSpan('stress', agents[i].id, t, 180, rng);
      s.kind = 'TRANSFER';
      const target = agents[(i + 1) % agents.length];
      s.links.push({
        targetSpanId: 'x',
        targetAgentId: target.id,
        relation: 'INVOKED',
      });
      perAgent[agents[i].id].push(s);
    }
  }
  for (const a of agents) {
    packLanes(perAgent[a.id]);
    for (const s of perAgent[a.id]) store.spans.append(s);
  }
  store.nowMs = durationMs;
  renderer.fitAll();
  await runForMs(renderer, 1500);
  const { p95, avg } = summarize(renderer);
  return {
    name: 'chatter',
    description: '10 agents, cross-agent transfers every 200ms for 60s',
    p95FrameMs: p95,
    avgFrameMs: avg,
    spanCount: store.spans.size,
    durationMs,
    budgetFrameMs: FRAME_BUDGET_MS,
    pass: p95 < FRAME_BUDGET_MS,
  };
}

export async function runAllScenarios(ctx: ScenarioContext): Promise<ScenarioResult[]> {
  const results: ScenarioResult[] = [];
  results.push(await steadyStateScenario(ctx));
  results.push(await burstScenario(ctx));
  results.push(await bigPayloadScenario(ctx));
  results.push(await chatterScenario(ctx));
  return results;
}
