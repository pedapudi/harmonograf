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
import type {
  TaskPlan as PbTaskPlan,
  Task as PbTask,
} from '../pb/harmonograf/v1/types_pb.js';
import type { TaskPlan, Task, TaskStatus } from '../gantt/types';

const TASK_STATUS_STRINGS: TaskStatus[] = [
  'UNSPECIFIED',
  'PENDING',
  'RUNNING',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
];

function taskStatusFromInt(n: number): TaskStatus {
  return TASK_STATUS_STRINGS[n] ?? 'UNSPECIFIED';
}

function tsToMsAbs(t: { seconds: bigint; nanos: number } | undefined): number {
  if (!t) return 0;
  return Number(t.seconds) * 1000 + Math.floor(t.nanos / 1_000_000);
}

function convertTask(t: PbTask): Task {
  return {
    id: t.id,
    title: t.title,
    description: t.description,
    assigneeAgentId: t.assigneeAgentId,
    status: taskStatusFromInt(t.status as unknown as number),
    predictedStartMs: Number(t.predictedStartMs),
    predictedDurationMs: Number(t.predictedDurationMs),
    boundSpanId: t.boundSpanId,
  };
}

function convertTaskPlan(p: PbTaskPlan, sessionStartMs: number): TaskPlan {
  const createdAbs = tsToMsAbs(p.createdAt);
  return {
    id: p.id,
    invocationSpanId: p.invocationSpanId,
    plannerAgentId: p.plannerAgentId,
    createdAtMs: createdAbs ? createdAbs - sessionStartMs : 0,
    summary: p.summary,
    tasks: p.tasks.map(convertTask),
    edges: p.edges.map((e) => ({ fromTaskId: e.fromTaskId, toTaskId: e.toTaskId })),
    revisionReason: p.revisionReason || '',
    revisionKind: p.revisionKind || '',
    revisionSeverity: p.revisionSeverity || '',
    revisionIndex: Number(p.revisionIndex ?? 0n),
  };
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

interface WatchSessionState {
  store: SessionStore;
  connected: boolean;
  initialBurstComplete: boolean;
  error: string | null;
}

// One SessionStore per sessionId, shared across hook consumers. Stores persist
// across hook unmounts for the lifetime of the tab — opening the same session
// a second time rejoins the existing stream.
const storeCache = new Map<string, SessionStore>();
const originCache = new Map<string, SessionOrigin>();
const refCounts = new Map<string, number>();
const abortControllers = new Map<string, AbortController>();

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
  });

  useEffect(() => {
    if (!sessionId) return;
    const store = getOrCreateStore(sessionId);
    stateRef.current = {
      store,
      connected: refCounts.get(sessionId) ? true : false,
      initialBurstComplete: false,
      error: null,
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

          switch (kind.case) {
            case 'session': {
              const s = kind.value;
              const startMs =
                (s.createdAt ? Number(s.createdAt.seconds) * 1000 : 0) || Date.now();
              origin = { startMs };
              originCache.set(sessionId, origin);
              store.wallClockStartMs = startMs;
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
            case 'taskPlan': {
              const sessionStart = origin?.startMs ?? 0;
              store.tasks.upsertPlan(convertTaskPlan(kind.value, sessionStart));
              break;
            }
            case 'updatedTaskStatus': {
              const u = kind.value;
              store.tasks.updateTaskStatus(
                u.planId,
                u.taskId,
                taskStatusFromInt(u.status as unknown as number),
                u.boundSpanId,
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

import {
  ControlKind,
  AnnotationKind,
} from '../pb/harmonograf/v1/types_pb.js';

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

export interface SendControlArgs {
  sessionId: string;
  agentId: string;
  spanId?: string;
  kind: keyof typeof CONTROL_KIND;
  payload?: Uint8Array;
}

export function useSendControl(): (args: SendControlArgs) => Promise<void> {
  return useMemo(() => {
    return async ({ sessionId, agentId, spanId, kind, payload }) => {
      const client = getHarmonografClient();
      await client.sendControl({
        sessionId,
        target: { agentId, spanId: spanId ?? '' },
        kind: CONTROL_KIND[kind] ?? ControlKind.UNSPECIFIED,
        payload: payload ?? new Uint8Array(0),
      });
    };
  }, []);
}

export async function sendStatusQuery(sessionId: string, agentId: string): Promise<string> {
  try {
    const client = getHarmonografClient();
    const resp = await client.sendControl({
      sessionId,
      target: { agentId, spanId: '' },
      kind: ControlKind.STATUS_QUERY,
      payload: new Uint8Array(0),
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
      const span = getSessionStore(sessionId)?.spans.get(spanId);
      const atMs = span?.startMs ?? 0;
      store.upsert({
        id: tempId,
        sessionId,
        spanId,
        agentId: span?.agentId ?? null,
        atMs,
        author,
        kind,
        body,
        createdAtMs: Date.now(),
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
