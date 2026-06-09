import { useEffect, useRef, useState } from 'react';
import { Shell } from './components/shell/Shell';
import { StressPage } from './gantt/StressPage';
import { useUiStore } from './state/uiStore';
import { sessionIdFromHash } from './lib/sessionRoute';

// Minimal hash router. The stress harness is dev-only and intentionally not
// linked anywhere user-facing. Visit /#/stress to open it.
function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash || '#/');
  useEffect(() => {
    const onHash = () => setHash(window.location.hash || '#/');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return hash;
}

// Reads a `#/session/<id>` deep link and selects that session in the UI store.
// Applies each distinct deep-link target exactly once — on the initial hash and
// again whenever the hash changes to a new session. The selection is eager: the
// id need not have arrived from ListSessions yet (WatchSession/ListSessions will
// populate it), and setting currentSessionId here also pre-empts SessionsSyncer's
// newest-first auto-select, so a deep link opens straight into its trace.
//
// Crucially this does NOT re-fire on every sessions poll: doing so would yank the
// user back to the deep-linked session ~every 5s after they navigate away via the
// picker (which updates the store, not the hash), trapping them on that session.
function useSessionDeepLink(hash: string): void {
  const setCurrentSession = useUiStore((s) => s.setCurrentSession);
  const wantedId = sessionIdFromHash(hash);
  const appliedId = useRef<string | null>(null);

  useEffect(() => {
    if (!wantedId) return;
    // Apply this target once; let a later manual selection (picker) stick.
    if (appliedId.current === wantedId) return;
    appliedId.current = wantedId;
    if (useUiStore.getState().currentSessionId === wantedId) return;
    setCurrentSession(wantedId);
  }, [wantedId, setCurrentSession]);
}

export default function App() {
  const hash = useHashRoute();
  useSessionDeepLink(hash);
  if (hash.startsWith('#/stress')) return <StressPage />;
  return <Shell />;
}
