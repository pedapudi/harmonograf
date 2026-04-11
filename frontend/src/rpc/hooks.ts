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
import { convertAgent, convertSpan, type SessionOrigin } from './convert';
import { packLanes } from '../gantt/layout';
import type { ListSessionsResponse } from '../pb/harmonograf/v1/frontend_pb.js';

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
                store.spans.update(existing);
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
      await client.postAnnotation({
        sessionId,
        target: { target: { case: 'spanId', value: spanId } },
        kind: kindEnum,
        body,
        author,
      });
    };
  }, []);
}
