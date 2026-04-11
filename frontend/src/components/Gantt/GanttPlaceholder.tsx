import { useEffect, useMemo } from 'react';
import { GanttCanvas } from '../../gantt/GanttCanvas';
import { SessionStore } from '../../gantt/index';
import { seedDemoSession } from '../../gantt/mockData';
import { useUiStore } from '../../state/uiStore';

// Demo surface for the Gantt while task #8 (WatchSession wiring) is in flight.
// Each session id gets its own SessionStore, seeded with synthetic spans.
const storeCache = new Map<string, SessionStore>();

function getStore(sessionId: string): SessionStore {
  let s = storeCache.get(sessionId);
  if (!s) {
    s = new SessionStore();
    seedDemoSession(s, { agents: 4, totalSpans: 400, durationMs: 5 * 60 * 1000 });
    storeCache.set(sessionId, s);
  }
  return s;
}

export function GanttPlaceholder() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const store = useMemo(
    () => (sessionId ? getStore(sessionId) : new SessionStore()),
    [sessionId],
  );

  // Advance the now cursor in live mode (visual only — no new spans).
  useEffect(() => {
    if (!sessionId) return;
    const start = performance.now();
    const t0 = store.nowMs;
    let handle = 0;
    const tick = () => {
      store.nowMs = t0 + (performance.now() - start);
      handle = requestAnimationFrame(tick);
    };
    handle = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(handle);
  }, [sessionId, store]);

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

  return <GanttCanvas store={store} />;
}
