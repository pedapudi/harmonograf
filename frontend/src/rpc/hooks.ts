// React hooks that bridge the generated Connect client to the rest of the
// frontend. Streaming RPCs feed the non-React SpanIndex store; unary RPCs
// return promises to the caller.
//
// The SessionStore cache keyed by sessionId lets multiple consumers share one
// live WatchSession subscription: opening the drawer while the Gantt is
// already watching reuses the same data path.

import { useEffect, useMemo, useRef, useState } from 'react';
import { ConnectError } from '@connectrpc/connect';
import { getHarmonografClient } from './client';
import { SessionStore } from '../gantt/index';
import {
  convertAgent,
  convertSpan,
  convertAttribute,
  convertPayloadRef,
  convertError,
  convertAnnotation,
  type SessionOrigin,
} from './convert';
import { useAnnotationStore } from '../state/annotationStore';
import { packLanes } from '../gantt/layout';
import type { ListSessionsResponse } from '../pb/harmonograf/v1/frontend_pb.js';
import { SessionStatus as PbSessionStatus } from '../pb/harmonograf/v1/types_pb.js';
import { applyGoldfiveEvent } from './goldfiveEvent';
import { loadPlanHistory } from '../state/planHistoryLoader';

// Translate the generated SessionStatus enum to the closed string set
// consumers actually care about. Unknown numeric values fall through to
// 'UNKNOWN' so a forward-compatible server can't blow the UI up.
function lifecycleFromPb(status: PbSessionStatus): SessionLifecycle {
  switch (status) {
    case PbSessionStatus.LIVE:
      return 'LIVE';
    case PbSessionStatus.COMPLETED:
      return 'COMPLETED';
    case PbSessionStatus.ABORTED:
      return 'ABORTED';
    default:
      return 'UNKNOWN';
  }
}

// --- Sessions list (polled) -------------------------------------------------

export interface SessionsState {
  sessions: ListSessionsResponse['sessions'];
  loading: boolean;
  error: string | null;
}

export function useSessions(pollIntervalMs = 5000): SessionsState {
  const [state, setState] = useState<SessionsState>({
    sessions: [],
    loading: true,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    const client = getHarmonografClient();

    const tick = async () => {
      try {
        const resp = await client.listSessions({});
        if (cancelled) return;
        setState({ sessions: resp.sessions, loading: false, error: null });
      } catch (e) {
        if (cancelled) return;
        const msg = e instanceof ConnectError ? e.message : String(e);
        setState((s) => ({ ...s, loading: false, error: msg }));
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(tick, pollIntervalMs);
        }
      }
    };
    tick();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [pollIntervalMs]);

  return state;
}

// --- Session watch (server-streaming) --------------------------------------

// Lifecycle status exposed to consumers. We translate the wire enum so UI
// code can switch on a small closed set without importing the generated
// pb.ts types everywhere. 'UNKNOWN' means the stream hasn't delivered the
// initial session payload yet.
export type SessionLifecycle = 'UNKNOWN' | 'LIVE' | 'COMPLETED' | 'ABORTED';

interface WatchSessionState {
  store: SessionStore;
  connected: boolean;
  initialBurstComplete: boolean;
  error: string | null;
  // Session lifecycle as of the last stream event that reported it. Starts
  // at 'UNKNOWN' before the initial session frame arrives; flips to
  // COMPLETED/ABORTED when SessionEnded is received or the server initially
  // reports an already-finished session during burst replay.
  sessionStatus: SessionLifecycle;
  // Wall-clock ms of the last event received on this stream. Consumers use
  // this as a heuristic for "still live" when the server side hasn't
  // transitioned status yet (e.g. if SessionEnded was never emitted).
  lastEventAtMs: number;
}

// One SessionStore per sessionId, shared across hook consumers. Stores persist
// across hook unmounts for the lifetime of the tab — opening the same session
// a second time rejoins the existing stream.
const storeCache = new Map<string, SessionStore>();
const originCache = new Map<string, SessionOrigin>();
const statusCache = new Map<string, SessionLifecycle>();
const lastEventCache = new Map<string, number>();
const refCounts = new Map<string, number>();
const abortControllers = new Map<string, AbortController>();
// Sessions whose plan-history backfill has already been attempted for
// this tab's lifetime. GetSessionPlanHistory is a single unary snapshot —
// firing it once per session is sufficient; the live event stream fans
// out plan_submitted / plan_revised deltas to the same registry from
// that point forward (both paths dedup on (plan_id, revision_number) in
// PlanHistoryRegistry.append). The set is keyed by sessionId rather than
// by subscription because a second consumer re-joining the same session
// already has the seeded registry.
const planHistoryBackfilled = new Set<string>();

function getOrCreateStore(sessionId: string): SessionStore {
  let s = storeCache.get(sessionId);
  if (!s) {
    s = new SessionStore();
    storeCache.set(sessionId, s);
  }
  return s;
}

// Lookup that other hooks/components can use to read the live store for a
// session without holding a WatchSession subscription. Returns undefined if
// no session is being watched.
export function getSessionStore(sessionId: string | null): SessionStore | undefined {
  if (!sessionId) return undefined;
  return storeCache.get(sessionId);
}

// Inactivity threshold (wall-clock ms) after which the heuristic treats a
// session as "effectively completed" even if the server hasn't emitted a
// SessionEnded yet. Kept generous so a momentarily chatty-but-slow agent
// doesn't flip the viewport to fit-all mid-run.
export const INACTIVITY_COMPLETED_MS = 30_000;

/**
 * True if a session is done (or effectively done) and the Gantt should stop
 * following a live cursor. Accepts the structure returned by
 * :func:`useSessionWatch` and a wall-clock reference for deterministic tests.
 * Returns false until the initial burst is complete so we don't autofit on a
 * fresh session whose first event hasn't arrived yet.
 */
export function sessionIsInactive(
  state: Pick<
    WatchSessionState,
    'sessionStatus' | 'lastEventAtMs' | 'initialBurstComplete' | 'connected'
  >,
  nowWallMs: number = Date.now(),
  inactivityMs: number = INACTIVITY_COMPLETED_MS,
): boolean {
  if (!state.initialBurstComplete) return false;
  if (state.sessionStatus === 'COMPLETED' || state.sessionStatus === 'ABORTED') {
    return true;
  }
  // Fallback: server didn't flip status but the stream has been quiet and
  // we've been disconnected long enough that any live session would have
  // heartbeated by now.
  if (
    !state.connected &&
    state.lastEventAtMs > 0 &&
    nowWallMs - state.lastEventAtMs > inactivityMs
  ) {
    return true;
  }
  return false;
}

// Reactive hook that re-renders whenever the agent registry changes for the
// given session. Returns the live Agent object for agentId, or null if not
// found. Use this instead of getSessionStore().agents.get() when the component
// needs to stay current with heartbeat/status updates.
export function useAgentLive(sessionId: string | null, agentId: string | null) {
  const store = getSessionStore(sessionId);
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!store) return;
    return store.agents.subscribe(() => setTick((t) => t + 1));
  }, [store]);
  if (!agentId) return null;
  return store?.agents.get(agentId) ?? null;
}

export function useSessionWatch(sessionId: string | null): WatchSessionState {
  const [tick, setTick] = useState(0);
  const stateRef = useRef<WatchSessionState>({
    store: new SessionStore(),
    connected: false,
    initialBurstComplete: false,
    error: null,
    sessionStatus: 'UNKNOWN',
    lastEventAtMs: 0,
  });

  useEffect(() => {
    if (!sessionId) return;
    const store = getOrCreateStore(sessionId);
    stateRef.current = {
      store,
      connected: refCounts.get(sessionId) ? true : false,
      initialBurstComplete: false,
      error: null,
      // Seed from module-level caches so a second consumer that joins an
      // already-open subscription sees the last reported status immediately,
      // not 'UNKNOWN' until the next event lands.
      sessionStatus: statusCache.get(sessionId) ?? 'UNKNOWN',
      lastEventAtMs: lastEventCache.get(sessionId) ?? 0,
    };
    setTick((n) => n + 1);

    const prev = refCounts.get(sessionId) ?? 0;
    refCounts.set(sessionId, prev + 1);
    if (prev > 0) {
      // Already watching — just rebuild lanes for the existing store view.
      return () => {
        const n = (refCounts.get(sessionId) ?? 1) - 1;
        if (n <= 0) {
          refCounts.delete(sessionId);
          abortControllers.get(sessionId)?.abort();
          abortControllers.delete(sessionId);
        } else {
          refCounts.set(sessionId, n);
        }
      };
    }

    const controller = new AbortController();
    abortControllers.set(sessionId, controller);
    const client = getHarmonografClient();
    const pending: Array<() => void> = [];

    (async () => {
      try {
        const stream = client.watchSession(
          { sessionId },
          { signal: controller.signal },
        );
        stateRef.current = { ...stateRef.current, connected: true };
        setTick((n) => n + 1);

        let origin: SessionOrigin | null = originCache.get(sessionId) ?? null;
        for await (const update of stream) {
          const kind = update.kind;
          if (!kind.case) continue;

          // Every payload counts as a liveness tick. We don't guard on kind
          // because all variants (including heartbeat-ish messages) prove
          // the server considers the session active.
          const nowWall = Date.now();
          lastEventCache.set(sessionId, nowWall);
          stateRef.current = { ...stateRef.current, lastEventAtMs: nowWall };
          let statusChanged = false;

          switch (kind.case) {
            case 'session': {
              const s = kind.value;
              const startMs =
                (s.createdAt ? Number(s.createdAt.seconds) * 1000 : 0) || Date.now();
              origin = { startMs };
              originCache.set(sessionId, origin);
              store.wallClockStartMs = startMs;
              // harmonograf#127: any DriftDetected / DelegationObserved
              // events that slipped in before this 'session' frame
              // landed were stamped with a wall-clock-scale relative
              // time (sessionStartMs was 0). Re-anchor them now that
              // the session start is known so the Gantt / Graph
              // delegation arrows and drift markers line up with the
              // rest of the timeline (which converts span start/end on
              // ingest via `origin`). Spans themselves don't need this
              // pass because they never read a zero `origin` — the
              // 'initialSpan' / 'newSpan' / 'endedSpan' arms all guard
              // with `if (!origin) origin = { startMs: 0 }` then
              // recompute, and the burst delivers the 'session' frame
              // before any span frame.
              store.rebaseRelativeTimestamps(startMs);
              // harmonograf: backfill PlanHistoryRegistry from the
              // GetSessionPlanHistory snapshot once we know the session
              // origin. On a COMPLETED session the live event stream
              // replays the plan as a single synthesized latest snapshot,
              // so without this unary RPC the Trajectory view's revision
              // strip only shows the final REV N. Idempotent on
              // (plan_id, revision_number) — if a live plan_submitted /
              // plan_revised also lands for the same revision (live run
              // being observed) it is deduped in planHistoryStore.append.
              // Best-effort: the loader swallows transport errors and a
              // client that predates the RPC short-circuits via its
              // `typeof getSessionPlanHistory === 'function'` guard.
              if (!planHistoryBackfilled.has(sessionId)) {
                planHistoryBackfilled.add(sessionId);
                void loadPlanHistory(sessionId, store, startMs).catch(
                  (err) => {
                    // Never fatal — a failure just means the view keeps
                    // running off whatever the live stream produces.
                    console.warn(
                      '[harmonograf] loadPlanHistory failed',
                      err,
                    );
                  },
                );
              }
              const nextStatus = lifecycleFromPb(s.status);
              if (statusCache.get(sessionId) !== nextStatus) {
                statusCache.set(sessionId, nextStatus);
                stateRef.current = { ...stateRef.current, sessionStatus: nextStatus };
                statusChanged = true;
              }
              break;
            }
            case 'sessionEnded': {
              const next = lifecycleFromPb(kind.value.finalStatus);
              // Treat any terminal transition as COMPLETED-ish if the
              // server didn't set final_status; better to auto-fit than
              // leave the viewport stuck following a live cursor that
              // will never advance again.
              const resolved = next === 'UNKNOWN' ? 'COMPLETED' : next;
              if (statusCache.get(sessionId) !== resolved) {
                statusCache.set(sessionId, resolved);
                stateRef.current = { ...stateRef.current, sessionStatus: resolved };
                statusChanged = true;
              }
              break;
            }
            case 'agent': {
              store.agents.upsert(convertAgent(kind.value));
              break;
            }
            case 'initialSpan': {
              if (!origin) origin = { startMs: 0 };
              store.spans.append(convertSpan(kind.value, origin));
              break;
            }
            case 'burstComplete': {
              // Rebuild lanes for every agent now that we have the burst data.
              for (const agent of store.agents.list) {
                const spans = store.spans.queryAgent(
                  agent.id,
                  -Number.MAX_SAFE_INTEGER,
                  Number.MAX_SAFE_INTEGER,
                );
                packLanes(spans);
              }
              stateRef.current = {
                ...stateRef.current,
                initialBurstComplete: true,
              };
              setTick((n) => n + 1);
              break;
            }
            case 'newSpan': {
              if (!origin) origin = { startMs: 0 };
              if (kind.value.span) {
                const ui = convertSpan(kind.value.span, origin);
                store.spans.append(ui);
              }
              break;
            }
            case 'updatedSpan': {
              const existing = store.spans.get(kind.value.spanId);
              if (existing) {
                const map: Record<number, (typeof existing)['status']> = {
                  0: 'PENDING',
                  1: 'PENDING',
                  2: 'RUNNING',
                  3: 'COMPLETED',
                  4: 'FAILED',
                  5: 'CANCELLED',
                  6: 'AWAITING_HUMAN',
                };
                existing.status = map[kind.value.status] ?? existing.status;
                for (const [k, v] of Object.entries(kind.value.attributes)) {
                  existing.attributes[k] = convertAttribute(v);
                }
                if (kind.value.payloadRefs.length > 0) {
                  existing.payloadRefs = kind.value.payloadRefs.map(convertPayloadRef);
                }
                store.spans.update(existing);
              }
              break;
            }
            case 'endedSpan': {
              const existing = store.spans.get(kind.value.spanId);
              if (existing) {
                if (kind.value.endTime && origin) {
                  existing.endMs =
                    Number(kind.value.endTime.seconds) * 1000 +
                    Math.floor(kind.value.endTime.nanos / 1_000_000) -
                    origin.startMs;
                }
                const map: Record<number, (typeof existing)['status']> = {
                  0: 'COMPLETED',
                  1: 'PENDING',
                  2: 'RUNNING',
                  3: 'COMPLETED',
                  4: 'FAILED',
                  5: 'CANCELLED',
                  6: 'AWAITING_HUMAN',
                };
                existing.status = map[kind.value.status] ?? existing.status;
                if (kind.value.error) {
                  existing.error = convertError(kind.value.error);
                }
                if (kind.value.payloadRefs.length > 0) {
                  existing.payloadRefs = kind.value.payloadRefs.map(convertPayloadRef);
                }
                store.spans.update(existing);
                // If an INVOCATION span just ended and the agent has no other
                // running INVOCATION spans, clear any stale taskReport so the
                // Graph view doesn't keep showing "Thinking: …" after the agent
                // finishes all work.
                if (existing.kind === 'INVOCATION') {
                  const agentSpans = store.spans.queryAgent(
                    existing.agentId,
                    -Number.MAX_SAFE_INTEGER,
                    Number.MAX_SAFE_INTEGER,
                  );
                  const hasRunningInvocation = agentSpans.some(
                    (s) => s.kind === 'INVOCATION' && s.endMs === null,
                  );
                  if (!hasRunningInvocation) {
                    store.agents.clearTaskReport(existing.agentId);
                  }
                }
              }
              break;
            }
            case 'initialAnnotation': {
              if (!origin) origin = { startMs: 0 };
              const span = kind.value.target?.target.case === 'spanId'
                ? store.spans.get(kind.value.target.target.value)
                : undefined;
              useAnnotationStore
                .getState()
                .upsert(convertAnnotation(kind.value, origin, span?.startMs));
              break;
            }
            case 'newAnnotation': {
              if (!origin) origin = { startMs: 0 };
              if (kind.value.annotation) {
                const ann = kind.value.annotation;
                const span = ann.target?.target.case === 'spanId'
                  ? store.spans.get(ann.target.target.value)
                  : undefined;
                useAnnotationStore
                  .getState()
                  .upsert(convertAnnotation(ann, origin, span?.startMs));
              }
              break;
            }
            case 'agentJoined': {
              if (kind.value.agent) {
                store.agents.upsert(convertAgent(kind.value.agent));
              }
              break;
            }
            case 'agentStatusChanged': {
              const m: Record<number, 'CONNECTED' | 'DISCONNECTED' | 'CRASHED'> = {
                0: 'DISCONNECTED',
                1: 'CONNECTED',
                2: 'DISCONNECTED',
                3: 'CRASHED',
              };
              store.agents.setStatus(
                kind.value.agentId,
                m[kind.value.status] ?? 'DISCONNECTED',
              );
              store.agents.setActivityAndStuck(
                kind.value.agentId,
                kind.value.currentActivity,
                kind.value.stuck,
              );
              break;
            }
            case 'goldfiveEvent': {
              // Every plan / task / drift delta rides on this oneof
              // case — the server replays the persisted plan + task
              // state during the initial burst as synthesized goldfive
              // events, and fans live bus deltas (DELTA_TASK_PLAN /
              // DELTA_TASK_STATUS / DELTA_DRIFT / DELTA_RUN_*) out on
              // the same stream (see server/.../rpc/frontend.py and
              // PR #14). Dispatch lives in goldfiveEvent.ts so the
              // translation is testable without standing up a mocked
              // Connect transport.
              applyGoldfiveEvent(
                kind.value,
                store,
                origin?.startMs ?? 0,
                sessionId,
              );
              break;
            }
            case 'taskReport': {
              const tr = kind.value;
              const recordedMs = tr.recordedAt
                ? Number(tr.recordedAt.seconds) * 1000 +
                  Math.floor(tr.recordedAt.nanos / 1_000_000)
                : Date.now();
              store.agents.setTaskReport(tr.agentId, tr.report, recordedMs);
              break;
            }
            case 'contextWindowSample': {
              // Task #2 wire → task #3 visualization seam. Convert the wall
              // clock Timestamp to session-relative ms (matching Span.startMs)
              // and narrow the int64 bigints to number — realistic token
              // counts are many orders of magnitude below 2^53.
              const cws = kind.value;
              if (!cws.recordedAt) break;
              const wallMs =
                Number(cws.recordedAt.seconds) * 1000 +
                Math.floor(cws.recordedAt.nanos / 1_000_000);
              const sessionStart = store.wallClockStartMs || 0;
              const tMs = sessionStart > 0 ? wallMs - sessionStart : wallMs;
              store.contextSeries.append(cws.agentId, {
                tMs,
                tokens: Number(cws.tokens),
                limitTokens: Number(cws.limitTokens),
              });
              break;
            }
            default:
              break;
          }
          if (statusChanged) {
            // Only rerender on lifecycle flips; per-event ticks would thrash
            // every consumer of the hook. Other event kinds already push
            // updates through the SessionStore subscribe channels that
            // React chrome subscribes to directly.
            setTick((n) => n + 1);
          }
        }
      } catch (e) {
        if (controller.signal.aborted) return;
        const msg = e instanceof ConnectError ? e.message : String(e);
        stateRef.current = { ...stateRef.current, error: msg, connected: false };
        setTick((n) => n + 1);
      }
    })();

    return () => {
      const n = (refCounts.get(sessionId) ?? 1) - 1;
      if (n <= 0) {
        refCounts.delete(sessionId);
        controller.abort();
        abortControllers.delete(sessionId);
      } else {
        refCounts.set(sessionId, n);
      }
      for (const fn of pending) fn();
    };
  }, [sessionId]);

  // tick forces a rerender when the stream state changes; consumers read
  // through stateRef so they always get the latest snapshot.
  void tick;
  return stateRef.current;
}

// --- Payload (lazy unary→server-streaming) ----------------------------------

export interface PayloadState {
  bytes: Uint8Array | null;
  mimeType: string;
  loading: boolean;
  error: string | null;
}

export function usePayload(digest: string | null): PayloadState {
  const [state, setState] = useState<PayloadState>({
    bytes: null,
    mimeType: '',
    loading: false,
    error: null,
  });

  useEffect(() => {
    if (!digest) {
      setState({ bytes: null, mimeType: '', loading: false, error: null });
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    setState({ bytes: null, mimeType: '', loading: true, error: null });
    (async () => {
      try {
        const client = getHarmonografClient();
        const stream = client.getPayload({ digest }, { signal: controller.signal });
        const chunks: Uint8Array[] = [];
        let mime = '';
        for await (const chunk of stream) {
          if (cancelled) return;
          if (chunk.mime) mime = chunk.mime;
          if (chunk.chunk?.length) chunks.push(chunk.chunk);
        }
        const total = chunks.reduce((a, b) => a + b.length, 0);
        const out = new Uint8Array(total);
        let o = 0;
        for (const c of chunks) {
          out.set(c, o);
          o += c.length;
        }
        if (!cancelled) {
          setState({ bytes: out, mimeType: mime, loading: false, error: null });
        }
      } catch (e) {
        if (cancelled) return;
        const msg = e instanceof ConnectError ? e.message : String(e);
        setState((s) => ({ ...s, loading: false, error: msg }));
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [digest]);

  return state;
}

// --- SendControl / PostAnnotation ------------------------------------------

import { AnnotationKind } from '../pb/harmonograf/v1/types_pb.js';
import { ControlKind } from '../pb/goldfive/v1/control_pb.js';

const CONTROL_KIND: Record<string, ControlKind> = {
  PAUSE: ControlKind.PAUSE,
  RESUME: ControlKind.RESUME,
  CANCEL: ControlKind.CANCEL,
  REWIND_TO: ControlKind.REWIND_TO,
  INJECT_MESSAGE: ControlKind.INJECT_MESSAGE,
  APPROVE: ControlKind.APPROVE,
  REJECT: ControlKind.REJECT,
  INTERCEPT_TRANSFER: ControlKind.INTERCEPT_TRANSFER,
  STEER: ControlKind.STEER,
};

export type ControlKindName = keyof typeof CONTROL_KIND;

/**
 * Arguments for :func:`useSendControl`. ``targetId`` is an APPROVE/REJECT
 * target (goldfive task_id for Flow A, ADK function-call id for Flow B);
 * ``note`` populates STEER; ``taskId`` populates REWIND_TO; ``role`` /
 * ``text`` populate INJECT_MESSAGE. Fields not applicable to the chosen
 * kind are ignored.
 */
export interface SendControlArgs {
  sessionId: string;
  agentId: string;
  toolCallId?: string;
  kind: ControlKindName;
  note?: string;
  suggestedAction?: string;
  taskId?: string;
  targetId?: string;
  detail?: string;
  role?: string;
  text?: string;
}

function buildControlEvent(args: SendControlArgs) {
  const kind = CONTROL_KIND[args.kind] ?? ControlKind.UNSPECIFIED;
  const event = {
    id: '',
    target: {
      agentId: args.agentId,
      taskId: '',
      toolCallId: args.toolCallId ?? '',
    },
    kind,
  } as Record<string, unknown>;
  switch (args.kind) {
    case 'STEER':
      event.payload = {
        case: 'steer',
        value: {
          note: args.note ?? '',
          suggestedAction: args.suggestedAction ?? '',
        },
      };
      break;
    case 'REWIND_TO':
      event.payload = {
        case: 'rewind',
        value: { taskId: args.taskId ?? '' },
      };
      break;
    case 'APPROVE':
      event.payload = {
        case: 'approve',
        value: {
          targetId: args.targetId ?? '',
          detail: args.detail ?? '',
        },
      };
      break;
    case 'REJECT':
      event.payload = {
        case: 'reject',
        value: {
          targetId: args.targetId ?? '',
          detail: args.detail ?? '',
        },
      };
      break;
    case 'INJECT_MESSAGE':
      event.payload = {
        case: 'injectMessage',
        value: {
          role: args.role ?? '',
          text: args.text ?? '',
        },
      };
      break;
    default:
      // PAUSE / RESUME / CANCEL / STATUS_QUERY / INTERCEPT_TRANSFER — no payload.
      break;
  }
  return event;
}

export function useSendControl(): (args: SendControlArgs) => Promise<void> {
  return useMemo(() => {
    return async (args) => {
      const client = getHarmonografClient();
      await client.sendControl({
        sessionId: args.sessionId,
        event: buildControlEvent(args),
      });
    };
  }, []);
}

export async function sendStatusQuery(sessionId: string, agentId: string): Promise<string> {
  try {
    const client = getHarmonografClient();
    const resp = await client.sendControl({
      sessionId,
      event: {
        id: '',
        target: { agentId, taskId: '', toolCallId: '' },
        kind: ControlKind.STATUS_QUERY,
      },
      ackTimeoutMs: 8000n,
    });
    // Return detail from the first ack.
    return resp.acks[0]?.detail ?? '';
  } catch {
    return '';
  }
}

export interface PostAnnotationArgs {
  sessionId: string;
  spanId: string;
  body: string;
  kind?: 'COMMENT' | 'STEERING' | 'HUMAN_RESPONSE';
  author?: string;
}

export function usePostAnnotation(): (args: PostAnnotationArgs) => Promise<void> {
  return useMemo(() => {
    return async ({ sessionId, spanId, body, kind = 'COMMENT', author = 'user' }) => {
      const client = getHarmonografClient();
      const kindEnum =
        kind === 'STEERING'
          ? AnnotationKind.STEERING
          : kind === 'HUMAN_RESPONSE'
            ? AnnotationKind.HUMAN_RESPONSE
            : AnnotationKind.COMMENT;

      // Optimistic insert. The store keys by id; on server ack we update the
      // same id with the canonical record; on failure we mark error so the
      // caller can retry or the pin can render in a failure state.
      const store = useAnnotationStore.getState();
      const tempId = `pending-${crypto.randomUUID?.() ?? Date.now().toString(36)}`;
      const sessionStore = getSessionStore(sessionId);
      const span = sessionStore?.spans.get(spanId);
      const atMs = span?.startMs ?? 0;
      // ``createdAtMs`` is session-relative ms, matching the value the
      // server ack path produces via ``convertAnnotation`` (issue #86).
      // Falls back to 0 when the session origin isn't known yet — the
      // pending row is replaced with the canonical ack row moments later
      // so the 0 never lands on the Trajectory view under normal
      // conditions.
      const sessionStartMs = sessionStore?.wallClockStartMs ?? 0;
      const createdAtMs = sessionStartMs > 0 ? Date.now() - sessionStartMs : 0;
      store.upsert({
        id: tempId,
        sessionId,
        spanId,
        agentId: span?.agentId ?? null,
        atMs,
        author,
        kind,
        body,
        createdAtMs,
        deliveredAtMs: null,
        pending: true,
        error: null,
      });

      try {
        const resp = await client.postAnnotation({
          sessionId,
          target: { target: { case: 'spanId', value: spanId } },
          kind: kindEnum,
          body,
          author,
        });
        // Replace the temp row with the canonical server-assigned row.
        store.remove(sessionId, tempId);
        if (resp.annotation) {
          const origin = originCache.get(sessionId) ?? { startMs: 0 };
          store.upsert(convertAnnotation(resp.annotation, origin, atMs));
        }
      } catch (e) {
        store.markError(sessionId, tempId, String(e));
        throw e;
      }
    };
  }, []);
}
