import { useEffect, useState } from 'react';
import { Shell } from './components/shell/Shell';
import { StressPage } from './gantt/StressPage';
import { useUiStore } from './state/uiStore';
import { useSessionsStore } from './state/sessionsStore';
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
// Runs on the initial hash and on every hashchange. Because the deep-linked id
// may not have arrived from ListSessions yet (sessions still loading), it
// records the desired id and (re)applies the selection whenever the sessions
// list changes — selecting the session as soon as it appears. Setting
// currentSessionId here also pre-empts SessionsSyncer's newest-session
// auto-select, so a deep link opens straight into its trace.
function useSessionDeepLink(hash: string): void {
  const setCurrentSession = useUiStore((s) => s.setCurrentSession);
  const sessions = useSessionsStore((s) => s.sessions);

  const wantedId = sessionIdFromHash(hash);

  useEffect(() => {
    if (!wantedId) return;
    // Already selected — nothing to do.
    const current = useUiStore.getState().currentSessionId;
    if (current === wantedId) return;
    // If the session is known, select it now. If not, select it eagerly
    // anyway: WatchSession/ListSessions will populate it, and this still
    // blocks SessionsSyncer's newest-first auto-select from racing ahead.
    setCurrentSession(wantedId);
  }, [wantedId, sessions, setCurrentSession]);
}

export default function App() {
  const hash = useHashRoute();
  useSessionDeepLink(hash);
  if (hash.startsWith('#/stress')) return <StressPage />;
  return <Shell />;
}
