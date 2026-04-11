// Synthetic data generators. Used by the dev stress harness and by the default
// session view until task #8 wires in real WatchSession.

import { packLanes } from './layout';
import { SessionStore } from './index';
import type { Agent, Span, SpanKind, SpanStatus } from './types';

let idCounter = 0;
function nextId(prefix: string): string {
  idCounter++;
  return `${prefix}-${idCounter}`;
}

export function makeAgent(name: string, joinMs: number): Agent {
  return {
    id: nextId('agent'),
    name,
    framework: 'ADK',
    capabilities: ['PAUSE_RESUME', 'CANCEL', 'REWIND', 'STEERING', 'HUMAN_IN_LOOP'],
    status: 'CONNECTED',
    connectedAtMs: joinMs,
  };
}

const KIND_MIX: SpanKind[] = [
  'LLM_CALL',
  'LLM_CALL',
  'LLM_CALL',
  'TOOL_CALL',
  'TOOL_CALL',
  'TOOL_CALL',
  'AGENT_MESSAGE',
  'USER_MESSAGE',
  'TRANSFER',
  'INVOCATION',
];

export function randomSpan(
  sessionId: string,
  agentId: string,
  startMs: number,
  durationMs: number,
  rng: () => number,
  status: SpanStatus = 'COMPLETED',
): Span {
  const kind = KIND_MIX[Math.floor(rng() * KIND_MIX.length)];
  return {
    id: nextId('span'),
    sessionId,
    agentId,
    parentSpanId: null,
    kind,
    status,
    name: `${kind.toLowerCase()}#${idCounter}`,
    startMs,
    endMs: status === 'RUNNING' ? null : startMs + durationMs,
    links: [],
    attributes: {},
    payloadRefs: [],
    error: null,
    lane: -1,
    replaced: false,
  };
}

export function seededRng(seed: number): () => number {
  // Mulberry32
  let t = seed >>> 0;
  return () => {
    t = (t + 0x6d2b79f5) >>> 0;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r;
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

// Generate a demo session with N agents and total M spans spread over `durationMs`.
export function seedDemoSession(
  store: SessionStore,
  opts: { agents: number; totalSpans: number; durationMs: number; seed?: number },
): void {
  const rng = seededRng(opts.seed ?? 42);
  const sessionId = 'demo-session';
  store.clear();
  for (let i = 0; i < opts.agents; i++) {
    store.agents.upsert(makeAgent(`agent-${i + 1}`, i * 250));
  }
  const agentList = store.agents.list;
  const perAgent = Math.floor(opts.totalSpans / agentList.length);
  for (const agent of agentList) {
    const spans: Span[] = [];
    for (let i = 0; i < perAgent; i++) {
      const start = Math.floor(rng() * opts.durationMs);
      const dur = Math.floor(rng() * 2000) + 50;
      spans.push(randomSpan(sessionId, agent.id, start, dur, rng));
    }
    // Occasionally mark a span AWAITING_HUMAN / FAILED / RUNNING.
    if (spans.length > 5) {
      spans[0].status = 'AWAITING_HUMAN';
      spans[0].endMs = null;
      spans[1].status = 'FAILED';
      spans[2].status = 'RUNNING';
      spans[2].endMs = null;
    }
    packLanes(spans);
    for (const s of spans) store.spans.append(s);
  }
  store.nowMs = opts.durationMs;
}
