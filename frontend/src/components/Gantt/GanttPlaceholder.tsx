import { useEffect, useMemo, useRef } from 'react';
import { GanttCanvas } from '../../gantt/GanttCanvas';
import { SessionStore } from '../../gantt/index';
import { seedDemoSession } from '../../gantt/mockData';
import { useUiStore } from '../../state/uiStore';
import { useSessionWatch, sessionIsInactive } from '../../rpc/hooks';
import { PinStrip } from '../Interaction/PinStrip';
import { RangeSelectionLayer } from '../Interaction/RangeSelectionLayer';
import { ApprovalEditor } from '../Interaction/ApprovalEditor';
import { AttentionSnackbar } from '../Interaction/AttentionSnackbar';
import { GanttDomProxy } from '../Interaction/GanttDomProxy';
import { SpanPopover } from '../Interaction/SpanPopover';
import { Minimap } from './Minimap';
import { ContextWindowBadgeStrip } from './ContextWindowBadgeStrip';

// When the server is reachable, the Gantt reads from the WatchSession-backed
// SessionStore owned by the rpc hooks module. When the server isn't reachable
// (or the session id is a demo id the server doesn't know), we fall back to a
// locally-seeded mock store so the frontend stays usable standalone.
const mockStoreCache = new Map<string, SessionStore>();

function getMockStore(sessionId: string): SessionStore {
  let s = mockStoreCache.get(sessionId);
  if (!s) {
    s = new SessionStore();
    seedDemoSession(s, { agents: 4, totalSpans: 400, durationMs: 5 * 60 * 1000 });
    mockStoreCache.set(sessionId, s);
  }
  return s;
}

export function GanttPlaceholder() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const activeRenderer = useUiStore((s) => s.activeRenderer);
  const watch = useSessionWatch(sessionId);
  const mock = useMemo(
    () => (sessionId ? getMockStore(sessionId) : new SessionStore()),
    [sessionId],
  );

  const useLive =
    !!sessionId && (watch.connected || watch.store.agents.size > 0);
  const store = useLive ? watch.store : mock;

  // Advance the now cursor in mock mode; in live mode the server drives it.
  useEffect(() => {
    if (!sessionId || useLive) return;
    const start = performance.now();
    const t0 = store.nowMs;
    let handle = 0;
    const tick = () => {
      store.nowMs = t0 + (performance.now() - start);
      handle = requestAnimationFrame(tick);
    };
    handle = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(handle);
  }, [sessionId, store, useLive]);

  // Completed-session autofit: when we load a session that's already done,
  // the default live-follow viewport happily advances wall-clock past the
  // last span and presents an empty canvas. Fit-all once per session so the
  // user lands on the whole run. We gate on `initialBurstComplete` so the
  // math sees real spans, not an empty index. `fitAll()` clears liveFollow,
  // and we only fire once per session id to avoid fighting subsequent user
  // pans. The ref key is sessionId-scoped so rejoining a different session
  // re-evaluates.
  const autofittedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!sessionId || !useLive || !activeRenderer) return;
    if (autofittedRef.current === sessionId) return;
    if (!watch.initialBurstComplete) return;
    if (!sessionIsInactive(watch)) return;
    // Guard: if the session has zero spans (e.g. a stillborn run), fitAll
    // would collapse to a 0-width window. Skip and let the default 5-min
    // viewport stand — there's nothing to look at anyway.
    if (store.spans.maxEndMs() <= 0) return;
    activeRenderer.fitAll();
    autofittedRef.current = sessionId;
  }, [
    sessionId,
    useLive,
    activeRenderer,
    watch,
    watch.initialBurstComplete,
    watch.sessionStatus,
    store,
  ]);

  if (!sessionId) {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--md-sys-color-on-surface-variant, #c3c6cf)',
          background: 'var(--md-sys-color-surface, #10131a)',
        }}
      >
        <div style={{ textAlign: 'center' }}>
          <p style={{ marginBottom: 8 }}>No session selected.</p>
          <p style={{ fontSize: 13, opacity: 0.7 }}>
            Open the session picker (⌘K) to pick one.
          </p>
        </div>
      </div>
    );
  }

  return (
    <>
      <GanttCanvas
        store={store}
        renderOverlay={(ctx) => (
          <>
            <GanttDomProxy ctx={ctx} />
            <RangeSelectionLayer ctx={ctx} sessionId={sessionId} />
            <PinStrip ctx={ctx} sessionId={sessionId} />
            <ApprovalEditor ctx={ctx} sessionId={sessionId} />
            <SpanPopover ctx={ctx} sessionId={sessionId} />
            <ContextWindowBadgeStrip ctx={ctx} />
            <Minimap ctx={ctx} />
          </>
        )}
      />
      <AttentionSnackbar store={store} sessionId={sessionId} />
    </>
  );
}
