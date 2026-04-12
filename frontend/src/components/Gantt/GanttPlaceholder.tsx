import { useEffect, useMemo } from 'react';
import { GanttCanvas } from '../../gantt/GanttCanvas';
import { SessionStore } from '../../gantt/index';
import { seedDemoSession } from '../../gantt/mockData';
import { useUiStore } from '../../state/uiStore';
import { useSessionWatch } from '../../rpc/hooks';
import { PinStrip } from '../Interaction/PinStrip';
import { RangeSelectionLayer } from '../Interaction/RangeSelectionLayer';
import { ApprovalEditor } from '../Interaction/ApprovalEditor';
import { AttentionSnackbar } from '../Interaction/AttentionSnackbar';
import { GanttDomProxy } from '../Interaction/GanttDomProxy';
import { SpanPopover } from '../Interaction/SpanPopover';
import { Minimap } from './Minimap';

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
            <Minimap ctx={ctx} />
          </>
        )}
      />
      <AttentionSnackbar store={store} sessionId={sessionId} />
    </>
  );
}
