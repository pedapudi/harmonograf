// adapter.ts — the REAL-data adapter for the zicato console.
//
// Maps the live `SessionStore` (gantt/index.ts) + the sessions list + plan
// hooks + annotations into the per-figure input bundle (`ZSession`) the zicato
// SVG renderers consume. NO study mock data.
//
// Time is converted ms → seconds here (the study renderers are in seconds).
// Every mapper has a documented graceful fallback so a figure never throws on
// absent data (see EMPTY_SESSION + the per-mapper notes).
//
// The single composing hook `useZicatoSession(sessionId)` is the only thing the
// views call; the pure mappers are exported so figure agents / unit tests can
// exercise them in isolation. The caller (ZicatoConsole) is responsible for
// holding the `useSessionWatch` that keeps the stream alive — this hook reads
// the store via `getSessionStore`.

import { useEffect, useMemo, useReducer } from 'react';
import { getSessionStore } from '../../rpc/hooks';
import { useSessionsStore } from '../../state/sessionsStore';
import { useUiStore } from '../../state/uiStore';
import {
  usePlanHistory,
  useCumulativePlan,
  useSupersedesMap,
} from '../../state/planHistoryHooks';
import type {
  CumulativePlan,
  PlanRevisionRecord,
  SupersessionLink,
} from '../../state/planHistoryStore';
import { useAnnotationStore, type Annotation } from '../../state/annotationStore';
import { deriveInterventionsFromStore } from '../../lib/interventions';
import { bareAgentName, type SessionStore } from '../../gantt/index';
import { extractThinkingText, hasThinking } from '../../lib/thinking';
import type {
  Span,
  SpanKind,
  SpanStatus,
  Task,
} from '../../gantt/types';
import {
  isSyntheticActor,
  actorDisplayLabel,
  USER_ACTOR_ID,
  GOLDFIVE_ACTOR_ID,
} from '../../theme/agentColors';

// ── 3.1 Output types — the figure-input contract (frozen) ────────────────────

/** Lower-kebab span kind matching the `--hg-kind-*` token names. */
export type ZKindToken =
  | 'invocation'
  | 'llm-call'
  | 'tool-call'
  | 'user-message'
  | 'agent-message'
  | 'transfer'
  | 'wait-for-human'
  | 'planned'
  | 'custom';

/** Lower treatment keys (status → treatment, not hue). */
export type ZStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'awaiting'
  | 'planned';

/** `--hg-gf-*` category for goldfive spans, or null for non-goldfive spans. */
export type ZGfClass =
  | 'judge-on-task'
  | 'judge-warning'
  | 'judge-critical'
  | 'judge-neutral'
  | 'refine'
  | 'plan'
  | 'reflective'
  | null;

export type ZSessionStatus = 'live' | 'done' | 'failed';

/** One agent lane. ordinal → `--hg-agent-N` (1..8); synthetics → -user/-goldfive. */
export interface ZAgent {
  /** real agentId ('<client>:<bare>' | '__user__' | '__goldfive__'). */
  id: string;
  /** bareAgentName / actorDisplayLabel. */
  label: string;
  /** 1..8 stable lane index; synthetic actors get 0 (resolved by colorVar). */
  ordinal: number;
  synthetic: 'user' | 'goldfive' | null;
}

/** One span bar (study `s.spans[]` shape, real-data-backed). */
export interface ZSpan {
  id: string;
  /** agent = ZAgent.id */
  agent: string;
  kind: ZKindToken;
  status: ZStatus;
  gf: ZGfClass;
  /** seconds, session-relative */
  t0: number;
  /** seconds, session-relative; equals `now` for running spans */
  t1: number;
  /** span.name */
  label: string;
  /**
   * True when the span carries model reasoning / chain-of-thought (detected
   * via lib/thinking.hasThinking — the `has_reasoning` flag or any
   * `llm.reasoning` / `llm.reasoning_trail` attribute). Drives the 🧠 glyph.
   */
  hasReasoning: boolean;
  /**
   * The reasoning text for this span (extractThinkingText), or null. The
   * INVOCATION-level `llm.reasoning_trail` aggregate is preferred over a
   * single LLM_CALL's `llm.reasoning`. Surfaced in the inspector + drawer.
   */
  reasoning: string | null;
}

/** Hand-off chord. seconds, agent ids. */
export interface ZTransfer {
  t: number;
  from: string;
  to: string;
}

/** Optional single delegation block. */
export interface ZDelegation {
  from: string;
  to: string;
  t0: number;
  t1: number;
  tokens: number | null;
  verdict: string | null;
}

export type ZArrowKind = 'transfer' | 'delegation' | 'return';

/** Derived gantt/sequence edge. */
export interface ZEdge {
  t: number;
  from: string;
  to: string;
  kind: ZArrowKind;
}

/**
 * A goldfive steering / correction event: a refine (or plan revision) the
 * orchestrator emitted in response to a drift, pointing at the agent/task it
 * steered. The gantt renders an arrow from the correction's moment on the
 * goldfive lane → the target agent's span at the steer time. Severity drives
 * the arrow hue (warning → --caution, critical → --bad).
 */
export interface ZSteer {
  /** seconds — when goldfive emitted the correction (refine recordedAt). */
  t: number;
  /** the goldfive lane id the arrow originates from. */
  from: string;
  /** the target agent id the correction steered (arrow lands here). */
  to: string;
  /** target task id, if the correction scoped one ('' otherwise). */
  taskId: string;
  /** lowercase drift kind that triggered the refine (e.g. 'off_topic'). */
  kind: string;
  /** lowercase severity of the trigger ('info'|'warning'|'critical'|''). */
  severity: string;
  /** short reason / detail text for the tooltip + drawer. */
  reason: string;
  /** revision number this correction produced, when resolvable (0 if not). */
  revision: number;
}

// Plan DAG + reel (study p.* shape).
export interface ZPlanNode {
  tid: string;
  title: string;
  x: number;
  y: number;
  st: 'done' | 'running' | 'pending' | 'added' | 'ghost';
}
export interface ZPlanEdge {
  from: string;
  to: string;
  crit: boolean;
}
export interface ZStratum {
  v: number;
  has: Set<string>;
  added: Set<string>;
  /** revision trigger label ('' if none) */
  seam: string;
  live: boolean;
}
export interface ZPlan {
  nodes: ZPlanNode[];
  edges: ZPlanEdge[];
  /** newest-first (study convention) */
  strata: ZStratum[];
  /** tasks-remaining per version, oldest→newest */
  rem: number[];
  planId: string | null;
}

// Seismograph / ladder.
/** agentId → [[t_sec, driftValue], …] */
export type ZJudges = Record<string, [number, number][]>;
/** agentId → [[t_sec, label], …] */
export type ZTicks = Record<string, [number, string][]>;
/** [[t_sec, rungIdx 0..3], …] */
export type ZLadder = [number, number][];
/** [[t_sec, fraction 0..1], …] */
export type ZCtx = [number, number][];

/** Fingerprint Lissajous params (deterministic derivation). */
export interface ZFingerprint {
  fx: number;
  fy: number;
  px: number;
  d: number;
  corrAt: number | null;
  grow: boolean;
  T: number;
}

/** The full per-session bundle the views destructure. */
export interface ZSession {
  id: string;
  /** goal = SessionSummary.title / Session.title (no separate goal field). */
  goal: string;
  status: ZSessionStatus;
  /** seconds */
  T: number;
  /** seconds */
  now: number;
  /** lane order (join-time, synthetic actors included) */
  agents: ZAgent[];
  spans: ZSpan[];
  transfers: ZTransfer[];
  delegation: ZDelegation | null;
  /** derived transfer/delegation/return edges (GraphView algo) */
  edges: ZEdge[];
  /** goldfive steering corrections → their target agent/task (drift-driven). */
  steers: ZSteer[];
  judges: ZJudges;
  ticks: ZTicks;
  ladder: ZLadder;
  ctx: ZCtx;
  plan: ZPlan;
  fp: ZFingerprint;
  /** true when no store/session → render placeholders */
  empty: boolean;
}

// ── Constants ────────────────────────────────────────────────────────────────

const EMPTY_PLAN: ZPlan = {
  nodes: [],
  edges: [],
  strata: [],
  rem: [],
  planId: null,
};

/** A tight default Lissajous knot (on-plan look) for sessions with no data. */
const DEFAULT_FP: ZFingerprint = {
  fx: 2,
  fy: 3,
  px: 0,
  d: 0.05,
  corrAt: null,
  grow: false,
  T: 30,
};

/** The placeholder bundle returned when no store/session is available. */
export const EMPTY_SESSION: ZSession = {
  id: '',
  goal: '',
  status: 'live',
  T: 30,
  now: 0,
  agents: [],
  spans: [],
  transfers: [],
  delegation: null,
  edges: [],
  steers: [],
  judges: {},
  ticks: {},
  ladder: [],
  ctx: [],
  plan: EMPTY_PLAN,
  fp: { ...DEFAULT_FP },
  empty: true,
};

/** Stable empty annotation list — keeps the zustand selector reference cached. */
const EMPTY_ANNOTATIONS: readonly Annotation[] = Object.freeze([]);

// DAG layout constants (study dagSVG).
const DAG_LX = [80, 300, 520, 740] as const;
const DAG_LY = (v: number): number => 92 + v * 44;

// ── 4-adjacent helper: severity → drift value (also re-exported from svgUtils) ─

/** Map a goldfive drift severity string to a seismograph drift value. */
export function severityToValue(sev: string): number {
  switch (sev) {
    case 'critical':
      return 14;
    case 'warning':
      return 9;
    case 'info':
      return 4;
    default:
      return 2;
  }
}

// ── 3.3 Pure mappers ─────────────────────────────────────────────────────────

/** UPPER_SNAKE SpanKind → lower-kebab kind token. 'LLM_CALL' → 'llm-call'. */
export function toKindToken(k: SpanKind): ZKindToken {
  switch (k) {
    case 'INVOCATION':
      return 'invocation';
    case 'LLM_CALL':
      return 'llm-call';
    case 'TOOL_CALL':
      return 'tool-call';
    case 'USER_MESSAGE':
      return 'user-message';
    case 'AGENT_MESSAGE':
      return 'agent-message';
    case 'TRANSFER':
      return 'transfer';
    case 'WAIT_FOR_HUMAN':
      return 'wait-for-human';
    case 'PLANNED':
      return 'planned';
    case 'CUSTOM':
      return 'custom';
    default:
      return 'custom';
  }
}

/** UPPER_SNAKE SpanStatus → lower treatment key. 'AWAITING_HUMAN' → 'awaiting'. */
export function toStatusToken(s: SpanStatus): ZStatus {
  switch (s) {
    case 'PENDING':
      return 'pending';
    case 'RUNNING':
      return 'running';
    case 'COMPLETED':
      return 'completed';
    case 'FAILED':
      return 'failed';
    case 'CANCELLED':
      return 'cancelled';
    case 'AWAITING_HUMAN':
      return 'awaiting';
    default:
      return 'completed';
  }
}

/**
 * Read a span's goldfive category from its attributes (if any). Looks for a
 * string attribute under a goldfive-category key and maps it onto a `--hg-gf-*`
 * class. Returns null for non-goldfive spans (the common case).
 */
export function gfClassForSpan(sp: Span): ZGfClass {
  const candidates = [
    'goldfive.category',
    'goldfive_category',
    'gf.category',
    'gf_category',
    'goldfive.class',
  ];
  let raw = '';
  for (const key of candidates) {
    const attr = sp.attributes[key];
    if (attr && attr.kind === 'string' && attr.value) {
      raw = attr.value;
      break;
    }
  }
  if (!raw) {
    // Goldfive's OWN llm spans (goal_derive / refine / judge_* / plan) carry no
    // category attribute, so they would all fall through to one llm-call hue.
    // Infer the sub-type from the span name — but ONLY for the goldfive actor,
    // so a work span that merely mentions "plan" is never miscoloured.
    const isGf =
      sp.agentId === GOLDFIVE_ACTOR_ID || sp.agentId.endsWith(':goldfive');
    if (!isGf) return null;
    const n = (sp.name ?? '').toLowerCase();
    if (n.includes('judge')) return 'judge-neutral';
    if (n.includes('refine') || n.includes('steer')) return 'refine';
    if (n.includes('plan')) return 'plan';
    return 'reflective'; // goal_derive and other goldfive own-ops
  }
  const v = raw.toLowerCase().replace(/_/g, '-');
  switch (v) {
    case 'judge-on-task':
    case 'judge-warning':
    case 'judge-critical':
    case 'judge-neutral':
    case 'refine':
    case 'plan':
    case 'reflective':
      return v;
    default:
      return null;
  }
}

/**
 * agents → lanes. Join-time order (store.agents.list). Each non-synthetic agent
 * gets a stable ordinal in 1..8 (`(index % 8) + 1`); the user/goldfive synthetic
 * actors are flagged so colorVar can route them to the -user/-goldfive tokens.
 * The goldfive row is canonicalized via store.resolveGoldfiveActorId().
 */
export function buildAgents(store: SessionStore): ZAgent[] {
  const goldfiveId = store.resolveGoldfiveActorId();
  const out: ZAgent[] = [];
  let nonSyntheticIdx = 0;
  for (const a of store.agents.list) {
    const synthetic = syntheticKind(a.id, goldfiveId);
    if (synthetic) {
      out.push({
        id: a.id,
        label: actorDisplayLabel(a.id) ?? synthetic,
        ordinal: 0,
        synthetic,
      });
    } else {
      const ordinal = (nonSyntheticIdx % 8) + 1;
      nonSyntheticIdx += 1;
      out.push({
        id: a.id,
        label: bareAgentName(a.id) || a.id,
        ordinal,
        synthetic: null,
      });
    }
  }
  return out;
}

function syntheticKind(
  agentId: string,
  goldfiveId: string,
): 'user' | 'goldfive' | null {
  if (agentId === USER_ACTOR_ID) return 'user';
  if (agentId === GOLDFIVE_ACTOR_ID || agentId === goldfiveId) return 'goldfive';
  if (agentId.endsWith(':goldfive')) return 'goldfive';
  // Fall back to the synthetic-actor helper for any other synthetic ids.
  const label = actorDisplayLabel(agentId);
  if (label === 'user') return 'user';
  if (label === 'goldfive') return 'goldfive';
  return isSyntheticActor(agentId) ? 'goldfive' : null;
}

/**
 * spans → bars. Queries the full session range. t0 = startMs/1000; running spans
 * (endMs == null) extend to `nowSec`. Replaced spans are kept (the renderer dims
 * them via status/opacity). Fallback: no spans → [].
 */
export function buildSpans(
  store: SessionStore,
  _agents: ZAgent[],
  nowSec: number,
): ZSpan[] {
  void _agents;
  const maxEnd = store.spans.maxEndMs();
  const spans = store.spans.queryRange(0, maxEnd + 1);
  const out: ZSpan[] = [];
  for (const sp of spans) {
    const t0 = sp.startMs / 1000;
    const t1 = sp.endMs != null ? sp.endMs / 1000 : nowSec;
    // Reasoning detection: reuse lib/thinking heuristics (has_reasoning flag
    // or any llm.reasoning / llm.reasoning_trail attribute). The 🧠 glyph and
    // the inspector/drawer reasoning block key off these two fields.
    out.push({
      id: sp.id,
      agent: sp.agentId,
      kind: toKindToken(sp.kind),
      status: toStatusToken(sp.status),
      gf: gfClassForSpan(sp),
      t0,
      t1: Math.max(t0, t1),
      label: sp.name,
      hasReasoning: hasThinking(sp),
      reasoning: extractThinkingText(sp),
    });
  }
  out.sort((a, b) => a.t0 - b.t0);
  return out;
}

/**
 * transfers → chords. Derived from the edge set (transfer-kind edges). Fallback:
 * none derivable → [].
 */
export function buildTransfers(store: SessionStore): ZTransfer[] {
  const out: ZTransfer[] = [];
  for (const e of buildEdges(store)) {
    if (e.kind === 'transfer') {
      out.push({ t: e.t, from: e.from, to: e.to });
    }
  }
  return out;
}

/**
 * delegation → optional single block. Picks the first DelegationRecord and
 * bounds it by the to-agent's first/last span when available. tokens/verdict are
 * left null in cut 1 (deferred, no canonical attribute). Fallback: no record → null.
 */
export function buildDelegation(store: SessionStore): ZDelegation | null {
  const recs = store.delegations.list();
  if (recs.length === 0) return null;
  const d = recs[0];
  if (!d.fromAgentId || !d.toAgentId) return null;
  const t0 = d.observedAtMs / 1000;
  // Approximate the delegated span window from the to-agent's spans after t0.
  const sub = store.spans
    .queryAgent(d.toAgentId, d.observedAtMs, Number.POSITIVE_INFINITY)
    .filter((s) => s.kind === 'INVOCATION');
  let t1 = t0;
  for (const s of sub) {
    const end = s.endMs != null ? s.endMs / 1000 : t0;
    if (end > t1) t1 = end;
  }
  return {
    from: d.fromAgentId,
    to: d.toAgentId,
    t0,
    t1: Math.max(t0, t1),
    tokens: null,
    verdict: null,
  };
}

/**
 * edges → transfer/delegation/return. Ported from GraphView.computeSequence
 * (map-data §5.6): Method1 (INVOCATION + INVOKED link), 1b (TRANSFER span carries
 * the link), 2 (cross-agent parent), 3 (store.delegations.list), + return edges.
 * Deduped on `<from>→<to>@round(ms/500)`. Times in seconds. Fallback: none → [].
 */
export function buildEdges(store: SessionStore): ZEdge[] {
  const allSpans = [...store.spans.all()];
  const spanById = new Map<string, Span>();
  for (const s of allSpans) spanById.set(s.id, s);

  interface Raw {
    fromId: string;
    toId: string;
    kind: 'transfer' | 'delegation';
    yMs: number;
  }
  const forward: Raw[] = [];
  const covered = new Set<string>();
  const keyFor = (from: string, to: string, ms: number): string =>
    `${from}→${to}@${Math.round(ms / 500)}`;

  // Method 1 — INVOCATION span with INVOKED link to a different agent.
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    for (const link of s.links) {
      if (link.relation !== 'INVOKED') continue;
      if (!link.targetAgentId || link.targetAgentId === s.agentId) continue;
      const anchorMs = s.startMs;
      covered.add(keyFor(link.targetAgentId, s.agentId, anchorMs));
      forward.push({
        fromId: link.targetAgentId,
        toId: s.agentId,
        kind: 'transfer',
        yMs: anchorMs,
      });
    }
  }
  // Method 1b — a TRANSFER span itself carries the INVOKED link.
  for (const s of allSpans) {
    if (s.kind !== 'TRANSFER') continue;
    for (const link of s.links) {
      if (link.relation !== 'INVOKED') continue;
      if (!link.targetAgentId || link.targetAgentId === s.agentId) continue;
      const targetSpan = link.targetSpanId
        ? spanById.get(link.targetSpanId)
        : undefined;
      const anchorMs = targetSpan ? targetSpan.startMs : s.startMs;
      const k = keyFor(s.agentId, link.targetAgentId, anchorMs);
      if (covered.has(k)) continue;
      covered.add(k);
      forward.push({
        fromId: s.agentId,
        toId: link.targetAgentId,
        kind: 'transfer',
        yMs: anchorMs,
      });
    }
  }
  // Method 2 — cross-agent INVOCATION parents (delegation).
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    if (!s.parentSpanId) continue;
    const parent = spanById.get(s.parentSpanId);
    if (!parent || parent.agentId === s.agentId) continue;
    const k = keyFor(parent.agentId, s.agentId, s.startMs);
    if (covered.has(k)) continue;
    covered.add(k);
    forward.push({
      fromId: parent.agentId,
      toId: s.agentId,
      kind: 'delegation',
      yMs: s.startMs,
    });
  }
  // Method 3 — goldfive DelegationObserved events.
  for (const d of store.delegations.list()) {
    if (!d.fromAgentId || !d.toAgentId) continue;
    if (d.fromAgentId === d.toAgentId) continue;
    const k = keyFor(d.fromAgentId, d.toAgentId, d.observedAtMs);
    if (covered.has(k)) continue;
    covered.add(k);
    forward.push({
      fromId: d.fromAgentId,
      toId: d.toAgentId,
      kind: 'delegation',
      yMs: d.observedAtMs,
    });
  }

  // Return edges — for each finished INVOCATION, find the most recent forward
  // arrow that targeted its agent before it started; emit a return at endMs.
  const forwardByDest = new Map<string, Raw[]>();
  for (const e of forward) {
    const arr = forwardByDest.get(e.toId) ?? [];
    arr.push(e);
    forwardByDest.set(e.toId, arr);
  }
  const returns: Raw[] = [];
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION') continue;
    if (s.endMs === null) continue;
    const incoming = forwardByDest.get(s.agentId) ?? [];
    const caller = incoming
      .filter((e) => e.yMs <= s.startMs)
      .sort((a, b) => b.yMs - a.yMs)[0];
    if (!caller) continue;
    if (caller.fromId === s.agentId) continue;
    returns.push({
      fromId: s.agentId,
      toId: caller.fromId,
      kind: 'delegation',
      yMs: s.endMs,
    });
  }

  const out: ZEdge[] = [];
  for (const e of forward) {
    out.push({ t: e.yMs / 1000, from: e.fromId, to: e.toId, kind: e.kind });
  }
  for (const e of returns) {
    out.push({ t: e.yMs / 1000, from: e.fromId, to: e.toId, kind: 'return' });
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}

/**
 * steers → goldfive correction arrows. A "steer" is a refine the orchestrator
 * emitted in response to a drift, pointing at the agent/task it corrected. We
 * model the arrow from the goldfive lane (at the refine moment) → the steered
 * agent. (Mirrors TrajectoryView.buildSteeringEvents but anchored in time so it
 * can ride the Gantt's lanes/zoom window instead of the plan DAG.)
 *
 * Target resolution, in priority order:
 *   1. the synthesized `refine:` span's `refine.target_agent_id` (matched by
 *      `refine.index == revision`) — the authoritative post-merge wire field.
 *   2. the RefineAttemptRecord's own `agentId` (the steered agent).
 *   3. the triggering DriftRecord's `agentId`.
 * The origin is always the goldfive lane. A steer with no resolvable, non-self
 * target agent is dropped (no arrow to draw).
 *
 * Primary source is `store.refineAttempts.list()`; when that registry is empty
 * (older sessions that never emitted RefineAttempted) we fall back to drifts
 * that triggered a plan revision (history.triggerEventId == driftId).
 */
export function buildSteers(
  store: SessionStore,
  history: readonly PlanRevisionRecord[],
): ZSteer[] {
  const goldfiveId = store.resolveGoldfiveActorId() || GOLDFIVE_ACTOR_ID;
  // Index refine spans on the goldfive row by refine.index so we can read the
  // target_agent_id the orchestrator stamped.
  const refineSpans: Span[] = [];
  store.spans.queryAgent(goldfiveId, 0, Number.POSITIVE_INFINITY, refineSpans);
  const targetByIndex = new Map<string, string>();
  for (const s of refineSpans) {
    if (!s.name.startsWith('refine:')) continue;
    const idx = readStringAttr(s, 'refine.index');
    const tgt = readStringAttr(s, 'refine.target_agent_id');
    if (idx && tgt) targetByIndex.set(idx, tgt);
  }
  // driftId → revision number, from the plan history (a plan revision triggered
  // by a drift carries triggerEventId == driftId).
  const revisionByDrift = new Map<string, number>();
  const reasonByDrift = new Map<string, string>();
  for (const rec of history) {
    if (rec.revision <= 0) continue;
    if (rec.triggerEventId) {
      if (!revisionByDrift.has(rec.triggerEventId)) {
        revisionByDrift.set(rec.triggerEventId, rec.revision);
      }
      if (rec.reason) reasonByDrift.set(rec.triggerEventId, rec.reason);
    }
  }
  const driftById = new Map<string, ReturnType<typeof driftToTuple>>();
  for (const d of store.drifts.list()) {
    if (d.driftId) driftById.set(d.driftId, driftToTuple(d));
  }

  const out: ZSteer[] = [];
  const attempts = store.refineAttempts.list();
  if (attempts.length > 0) {
    for (const a of attempts) {
      const revision =
        revisionByDrift.get(a.driftId) ?? 0;
      const target =
        (revision ? targetByIndex.get(String(revision)) : undefined) ||
        a.agentId ||
        driftById.get(a.driftId)?.agentId ||
        '';
      if (!target || target === goldfiveId) continue;
      out.push({
        t: a.recordedAtMs / 1000,
        from: goldfiveId,
        to: target,
        taskId: a.taskId || driftById.get(a.driftId)?.taskId || '',
        kind: a.triggerKind || driftById.get(a.driftId)?.kind || '',
        severity: a.triggerSeverity || driftById.get(a.driftId)?.severity || '',
        reason:
          reasonByDrift.get(a.driftId) || driftById.get(a.driftId)?.detail || '',
        revision,
      });
    }
  } else {
    // Fallback: drifts that produced a plan revision (no RefineAttempted rows).
    for (const d of store.drifts.list()) {
      const revision = d.driftId ? revisionByDrift.get(d.driftId) ?? 0 : 0;
      if (revision <= 0) continue;
      const target =
        targetByIndex.get(String(revision)) || d.agentId || '';
      if (!target || target === goldfiveId) continue;
      out.push({
        t: d.recordedAtMs / 1000,
        from: goldfiveId,
        to: target,
        taskId: d.taskId || '',
        kind: d.kind || '',
        severity: d.severity || '',
        reason: reasonByDrift.get(d.driftId) || d.detail || '',
        revision,
      });
    }
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}

/** Pull a string attribute off a span (helper for the steer/target lookups). */
function readStringAttr(span: Span | null, key: string): string {
  if (!span) return '';
  const attr = span.attributes[key];
  if (!attr || attr.kind !== 'string') return '';
  return attr.value;
}

/** Narrow a DriftRecord to the fields buildSteers reads (keeps the Map typed). */
function driftToTuple(d: {
  agentId: string;
  taskId: string;
  kind: string;
  severity: string;
  detail: string;
}): { agentId: string; taskId: string; kind: string; severity: string; detail: string } {
  return {
    agentId: d.agentId,
    taskId: d.taskId,
    kind: d.kind,
    severity: d.severity,
    detail: d.detail,
  };
}

/**
 * judges → per-agent drift trace keyframes. From store.drifts.list(): push
 * [recordedAtMs/1000, severityToValue(severity)] per drift, grouped by agentId
 * (falling back to the goldfive lane when a drift has no agent). Fallback: no
 * drift → {} (seismograph baseline only).
 */
export function buildJudges(store: SessionStore, agents: ZAgent[]): ZJudges {
  void agents;
  const goldfiveId = store.resolveGoldfiveActorId();
  const out: ZJudges = {};
  for (const d of store.drifts.list()) {
    const agent = d.agentId || goldfiveId;
    if (!agent) continue;
    const arr = out[agent] ?? (out[agent] = []);
    arr.push([d.recordedAtMs / 1000, severityToValue(d.severity)]);
  }
  for (const k of Object.keys(out)) out[k].sort((a, b) => a[0] - b[0]);
  return out;
}

/**
 * ticks → intervention markers per agent. deriveInterventionsFromStore grouped
 * by targetAgentId (fallback the goldfive lane), [atMs/1000, kind]. Fallback: no
 * interventions → {}.
 */
export function buildTicks(
  store: SessionStore,
  annotations: readonly Annotation[],
): ZTicks {
  const goldfiveId = store.resolveGoldfiveActorId() || GOLDFIVE_ACTOR_ID;
  const rows = deriveInterventionsFromStore(store, annotations);
  const out: ZTicks = {};
  for (const r of rows) {
    const agent = r.targetAgentId || goldfiveId;
    const arr = out[agent] ?? (out[agent] = []);
    arr.push([r.atMs / 1000, r.kind || r.source]);
  }
  for (const k of Object.keys(out)) out[k].sort((a, b) => a[0] - b[0]);
  return out;
}

/**
 * ladder → intervention-ladder dots. Maps each intervention row's kind/severity
 * to a rung: nudge(0) / refine(1) / replan(2) / escalate(3). escalate when the
 * severity is critical or the row came from a goldfive escalation. Fallback: no
 * interventions → [] ("never left the ground").
 */
export function buildLadder(
  store: SessionStore,
  annotations: readonly Annotation[],
): ZLadder {
  const rows = deriveInterventionsFromStore(store, annotations);
  const out: ZLadder = [];
  for (const r of rows) {
    out.push([r.atMs / 1000, rungForRow(r.source, r.kind, r.severity, r.outcome)]);
  }
  out.sort((a, b) => a[0] - b[0]);
  return out;
}

function rungForRow(
  source: string,
  kind: string,
  severity: string,
  outcome: string,
): number {
  if (severity === 'critical') return 3; // escalate
  const k = (kind || '').toLowerCase();
  if (k.includes('escalat')) return 3;
  if (source === 'refine' || k.includes('refine')) return 1;
  if (
    source === 'transition' ||
    k.includes('replan') ||
    k.includes('revis') ||
    outcome.startsWith('plan_revised')
  ) {
    return 2;
  }
  return 0; // nudge / steer / comment
}

/**
 * ctx → context-window utilisation curve for the busiest non-synthetic agent.
 * store.contextSeries.forAgent(id) → [tMs/1000, tokens/limitTokens]. Fallback: no
 * samples → [].
 */
export function buildCtx(store: SessionStore, agents: ZAgent[]): ZCtx {
  // Pick the busiest non-synthetic agent (most spans).
  let bestId: string | null = null;
  let bestCount = -1;
  for (const a of agents) {
    if (a.synthetic) continue;
    const count = store.spans.queryAgent(
      a.id,
      0,
      Number.POSITIVE_INFINITY,
    ).length;
    if (count > bestCount) {
      bestCount = count;
      bestId = a.id;
    }
  }
  if (!bestId) return [];
  const samples = store.contextSeries.forAgent(bestId);
  const out: ZCtx = [];
  for (const s of samples) {
    if (s.limitTokens <= 0) continue;
    out.push([s.tMs / 1000, Math.min(1, s.tokens / s.limitTokens)]);
  }
  return out;
}

/**
 * plan → DAG + reel. Lays cumulative.tasks into 4 cols by topo-depth (LX/LY),
 * derives node state from task.status + revision meta, edges from cumulative.edges
 * with a longest-path "critical" heuristic, and strata (newest-first) + rem from
 * the revision history. Fallback: no planId → empty ZPlan.
 */
export function buildPlan(
  history: readonly PlanRevisionRecord[],
  cumulative: CumulativePlan | null,
  supersedes: Map<string, SupersessionLink>,
  selectedRevision: number | null,
): ZPlan {
  if (!cumulative || !cumulative.id) return { ...EMPTY_PLAN };
  const planId = cumulative.id;
  const tasks = cumulative.tasks;
  const meta = cumulative.taskRevisionMeta;
  // The newest revision index — from the history (CumulativePlan extends
  // TaskPlan and has no explicit latestRevisionIndex field).
  const latestRev = history.reduce((m, r) => Math.max(m, r.revision), 0);
  const selRev = selectedRevision;

  // ── topological depth for column placement ───────────────────────────────
  const depth = topoDepths(
    tasks.map((t) => t.id),
    cumulative.edges,
  );
  const perCol: Record<number, number> = {};
  const nodes: ZPlanNode[] = tasks.map((t) => {
    const d = Math.min(DAG_LX.length - 1, depth.get(t.id) ?? 0);
    const rowInCol = perCol[d] ?? 0;
    perCol[d] = rowInCol + 1;
    return {
      tid: t.id,
      title: t.title || t.id,
      x: DAG_LX[d],
      y: DAG_LY(rowInCol),
      st: nodeState(t, meta.get(t.id)?.isSuperseded ?? false, supersedes),
    };
  });

  // ── critical-path heuristic: longest dependency chain ────────────────────
  const critEdges = criticalEdgeSet(
    tasks.map((t) => t.id),
    cumulative.edges,
  );
  const edges: ZPlanEdge[] = cumulative.edges.map((e) => ({
    from: e.fromTaskId,
    to: e.toTaskId,
    crit: critEdges.has(`${e.fromTaskId}→${e.toTaskId}`),
  }));

  // ── strata (newest-first) + rem (oldest→newest) ──────────────────────────
  const strata: ZStratum[] = [];
  const rem: number[] = [];
  const ordered = [...history].sort((a, b) => a.revision - b.revision);
  let prevIds = new Set<string>();
  for (const rec of ordered) {
    const ids = new Set(rec.plan.tasks.map((t) => t.id));
    const added = new Set<string>();
    for (const id of ids) if (!prevIds.has(id)) added.add(id);
    strata.push({
      v: rec.revision,
      has: ids,
      added,
      seam: seamLabel(rec.kind),
      live: rec.revision === latestRev,
    });
    rem.push(
      rec.plan.tasks.filter((t) => !isTerminalTaskStatus(t.status)).length,
    );
    prevIds = ids;
  }
  strata.reverse(); // newest-first (study convention)

  // Tag added nodes for the selected revision so DagZ can render added/ghost.
  if (selRev != null) {
    const stratum = strata.find((s) => s.v === selRev);
    if (stratum) {
      for (const n of nodes) {
        if (stratum.added.has(n.tid) && n.st !== 'done') n.st = 'added';
        else if (!stratum.has.has(n.tid)) n.st = 'ghost';
      }
    }
  }

  return { nodes, edges, strata, rem, planId };
}

function nodeState(
  task: Task,
  isSuperseded: boolean,
  supersedes: Map<string, SupersessionLink>,
): ZPlanNode['st'] {
  if (isSuperseded || supersedes.has(task.id)) return 'ghost';
  switch (task.status) {
    case 'COMPLETED':
      return 'done';
    case 'RUNNING':
      return 'running';
    case 'FAILED':
    case 'CANCELLED':
      return 'ghost';
    default:
      return 'pending';
  }
}

function isTerminalTaskStatus(status: string): boolean {
  return (
    status === 'COMPLETED' || status === 'FAILED' || status === 'CANCELLED'
  );
}

function seamLabel(kind: string): string {
  const k = (kind || '').toLowerCase();
  if (!k) return '';
  if (k.includes('off_topic') || k.includes('off-topic')) return 'goal-drift';
  if (k.includes('scope')) return 'scope-creep';
  if (k.includes('tool')) return 'tool-misuse';
  if (k.includes('user_steer') || k.includes('user-steer')) return 'user-steer';
  return k.replace(/_/g, '-');
}

/** Topological depth (0-based) for each node id over the given edges. */
function topoDepths(
  ids: string[],
  edges: readonly { fromTaskId: string; toTaskId: string }[],
): Map<string, number> {
  const adj = new Map<string, string[]>();
  const indeg = new Map<string, number>();
  for (const id of ids) {
    adj.set(id, []);
    indeg.set(id, 0);
  }
  for (const e of edges) {
    if (!adj.has(e.fromTaskId) || !indeg.has(e.toTaskId)) continue;
    adj.get(e.fromTaskId)!.push(e.toTaskId);
    indeg.set(e.toTaskId, (indeg.get(e.toTaskId) ?? 0) + 1);
  }
  const depth = new Map<string, number>();
  const queue: string[] = [];
  for (const id of ids) {
    if ((indeg.get(id) ?? 0) === 0) {
      depth.set(id, 0);
      queue.push(id);
    }
  }
  while (queue.length) {
    const cur = queue.shift()!;
    const cd = depth.get(cur) ?? 0;
    for (const next of adj.get(cur) ?? []) {
      const nd = cd + 1;
      const indegNext = (indeg.get(next) ?? 0) - 1;
      indeg.set(next, indegNext);
      if ((depth.get(next) ?? -1) < nd) depth.set(next, nd);
      if (indegNext === 0) queue.push(next);
    }
  }
  for (const id of ids) if (!depth.has(id)) depth.set(id, 0);
  return depth;
}

/**
 * The set of edges on the longest path through the DAG (the "critical path").
 * Returns a set of `<from>→<to>` keys. A pragmatic heuristic matching the study
 * dagSVG spine.
 */
function criticalEdgeSet(
  ids: string[],
  edges: readonly { fromTaskId: string; toTaskId: string }[],
): Set<string> {
  const adj = new Map<string, string[]>();
  for (const id of ids) adj.set(id, []);
  for (const e of edges) {
    if (adj.has(e.fromTaskId)) adj.get(e.fromTaskId)!.push(e.toTaskId);
  }
  const depth = topoDepths(ids, edges);
  // best[id] = [length, predecessor] of the longest path ending at id.
  const best = new Map<string, { len: number; prev: string | null }>();
  const order = [...ids].sort(
    (a, b) => (depth.get(a) ?? 0) - (depth.get(b) ?? 0),
  );
  for (const id of order) best.set(id, { len: 0, prev: null });
  let endId: string | null = null;
  let endLen = -1;
  for (const id of order) {
    const cur = best.get(id)!;
    for (const next of adj.get(id) ?? []) {
      const cand = cur.len + 1;
      const nb = best.get(next);
      if (nb && cand > nb.len) {
        best.set(next, { len: cand, prev: id });
      }
    }
    if (cur.len > endLen) {
      endLen = cur.len;
      endId = id;
    }
  }
  const out = new Set<string>();
  let walk = endId;
  while (walk) {
    const node = best.get(walk);
    if (!node || node.prev == null) break;
    out.add(`${node.prev}→${walk}`);
    walk = node.prev;
  }
  return out;
}

/**
 * fingerprint → deterministic Lissajous params (no Math.random). Base fx=2,
 * fy=3, px=0. Damping `d` rises with on-plan-ness (fewer revisions + more
 * completed tasks → tighter knot). grow=true when revisions keep adding scope.
 * corrAt is the first revision time (a correction kink) when there is exactly one
 * steer. Fallback: no revisions → tight default Lissajous. (Documented first
 * pass — the ONE figure with no direct real source.)
 */
export function deriveFingerprint(
  history: readonly PlanRevisionRecord[],
  cumulative: CumulativePlan | null,
  status: ZSessionStatus,
  T: number,
): ZFingerprint {
  void status;
  const revisions = Math.max(0, history.length - 1); // beyond the initial plan
  const tasks = cumulative?.tasks ?? [];
  const total = tasks.length;
  const completed = tasks.filter((t) => t.status === 'COMPLETED').length;
  const completion = total > 0 ? completed / total : 0;

  // More revisions / lower completion → looser knot (smaller damping).
  // Few revisions + high completion → tighter (higher damping).
  const base = 0.02;
  const d = Math.max(
    0.01,
    base + 0.06 * completion - 0.018 * Math.min(revisions, 4),
  );

  // Diverging: scope grows monotonically across revisions.
  let grow = false;
  if (history.length >= 2) {
    const sizes = [...history]
      .sort((a, b) => a.revision - b.revision)
      .map((r) => r.plan.tasks.length);
    grow = sizes.every((s, i) => i === 0 || s >= sizes[i - 1]) &&
      sizes[sizes.length - 1] > sizes[0];
  }

  // Exactly one steer → a single correction kink at its revision time.
  let corrAt: number | null = null;
  if (revisions === 1) {
    const steer = [...history]
      .sort((a, b) => a.revision - b.revision)
      .find((r) => r.revision > 0);
    if (steer) {
      const tSec = steer.plan.createdAtMs / 1000;
      corrAt = T > 0 ? Math.max(0, Math.min(T, tSec)) : tSec;
    }
  }

  return { fx: 2, fy: 3, px: 0, d, corrAt, grow, T: T > 0 ? T : 30 };
}

/** synthetic → `--hg-agent-user`/`-goldfive`; else `--hg-agent-<ordinal>`. */
export function colorVar(a: ZAgent): string {
  if (a.synthetic === 'user') return 'var(--hg-agent-user)';
  if (a.synthetic === 'goldfive') return 'var(--hg-agent-goldfive)';
  const ord = a.ordinal >= 1 && a.ordinal <= 8 ? a.ordinal : 1;
  return `var(--hg-agent-${ord})`;
}

// ── 3.2 / 3.4 The composing hook ─────────────────────────────────────────────

/**
 * The per-session bundle for the zicato views. Reads the live SessionStore via
 * getSessionStore (the caller holds the watch), subscribes to the registry
 * channels for reactivity, and composes the figure inputs inside a useMemo.
 *
 * Time derivation (map-data §1.4 gotcha — do NOT trust store.nowMs):
 *   T   = store.spans.maxEndMs() / 1000
 *   now = max(T, (Date.now() - wallClockStartMs) / 1000), clamped ≥ 0.
 */
export function useZicatoSession(sessionId: string | null): ZSession {
  const store = getSessionStore(sessionId);

  // Reactivity + a captured wall-clock timestamp. We snapshot `Date.now()` in
  // the reducer / lazy initializer (NOT in render), so the memo derives `now`
  // purely from `tick.nowMs`. Every `bump()` (registry change OR the 1s timer)
  // writes a fresh nowMs, which drives both reactivity and the advancing
  // play-head for running spans.
  const [tick, bump] = useReducer(
    (s: { n: number; nowMs: number }) => ({ n: s.n + 1, nowMs: Date.now() }),
    0,
    (): { n: number; nowMs: number } => ({ n: 0, nowMs: Date.now() }),
  );
  useEffect(() => {
    if (!store) return;
    const uns = [
      store.spans.subscribe(() => bump()),
      store.agents.subscribe(() => bump()),
      store.tasks.subscribe(() => bump()),
      store.drifts.subscribe(() => bump()),
      store.delegations.subscribe(() => bump()),
      store.contextSeries.subscribe(() => bump()),
    ];
    const timer = setInterval(() => bump(), 1000);
    return () => {
      clearInterval(timer);
      for (const un of uns) un();
    };
  }, [store]);

  // Read the per-session annotation list. Falls back to a STABLE empty array
  // (EMPTY_ANNOTATIONS) so the zustand selector returns a cached reference when
  // there are none — a fresh `[]` each render would trip useSyncExternalStore's
  // infinite-loop guard.
  const annotations = useAnnotationStore((s) =>
    sessionId ? s.bySession.get(sessionId) ?? EMPTY_ANNOTATIONS : EMPTY_ANNOTATIONS,
  );

  // Session goal (title) from the sessions list, AppBar-style fallback.
  const sessions = useSessionsStore((s) => s.sessions);
  const goal = useMemo(() => {
    if (!sessionId) return '';
    return sessions.find((s) => s.id === sessionId)?.title ?? '';
  }, [sessions, sessionId]);

  // Plan inputs are hook-fed (frozen plan hooks; safe with null planId).
  const trajectoryPlanId = useUiStore((s) => s.trajectorySelectedPlanId);
  const planId =
    trajectoryPlanId ?? store?.tasks.listPlans()[0]?.id ?? null;
  const history = usePlanHistory(sessionId, planId);
  const cumulative = useCumulativePlan(sessionId, planId);
  const supersedes = useSupersedesMap(sessionId, planId);
  const selectedRevision = useUiStore((s) => s.selectedRevision);

  return useMemo(() => {
    if (!store) return EMPTY_SESSION;
    const maxEndMs = store.spans.maxEndMs();
    const spanCount = [...store.spans.all()].length;
    if (spanCount === 0 && store.agents.list.length === 0) {
      // Graceful empty: no observable data yet.
      const fp = deriveFingerprint(history, cumulative, 'live', 30);
      return { ...EMPTY_SESSION, id: sessionId ?? '', goal, fp };
    }

    const T = maxEndMs / 1000;
    const wallNowSec = (tick.nowMs - store.wallClockStartMs) / 1000;
    const now = Math.max(0, Math.max(T, wallNowSec));

    const status = deriveStatus(store);
    const agents = buildAgents(store);
    const spans = buildSpans(store, agents, now);
    const edges = buildEdges(store);
    const transfers: ZTransfer[] = edges
      .filter((e) => e.kind === 'transfer')
      .map((e) => ({ t: e.t, from: e.from, to: e.to }));
    const delegation = buildDelegation(store);
    const steers = buildSteers(store, history);
    const judges = buildJudges(store, agents);
    const ticks = buildTicks(store, annotations);
    const ladder = buildLadder(store, annotations);
    const ctx = buildCtx(store, agents);
    const plan = buildPlan(history, cumulative, supersedes, selectedRevision);
    const fp = deriveFingerprint(history, cumulative, status, T || 30);

    return {
      id: sessionId ?? '',
      goal,
      status,
      T: T > 0 ? T : 30,
      now,
      agents,
      spans,
      transfers,
      delegation,
      edges,
      steers,
      judges,
      ticks,
      ladder,
      ctx,
      plan,
      fp,
      empty: false,
    };
    // tick.nowMs is included so each registry subscribe() bump + the 1s timer
    // (both write a fresh nowMs) recompute the bundle and advance `now`.
  }, [
    store,
    sessionId,
    tick.nowMs,
    annotations,
    goal,
    history,
    cumulative,
    supersedes,
    selectedRevision,
  ]);
}

/**
 * Map the live session into the study's `'live' | 'done' | 'failed'` status.
 * A session with any failed (non-replaced) span and no running spans reads as
 * failed; once everything is terminal it reads done; otherwise live.
 */
function deriveStatus(store: SessionStore): ZSessionStatus {
  let anyRunning = false;
  let anyFailed = false;
  let anyOpen = false;
  for (const s of store.spans.all()) {
    if (s.status === 'RUNNING') anyRunning = true;
    if (s.status === 'FAILED' && !s.replaced) anyFailed = true;
    if (s.status === 'PENDING' || s.status === 'AWAITING_HUMAN') anyOpen = true;
    if (s.endMs === null) anyOpen = true;
  }
  if (anyRunning || anyOpen) return 'live';
  if (anyFailed) return 'failed';
  return 'done';
}
