import { useEffect, useMemo } from 'react';
import { useSessions } from './hooks';
import { useSessionsStore, sessionCreatedAtMs } from '../state/sessionsStore';
import { useUiStore } from '../state/uiStore';
import { sessionFilterRouteFromHash } from '../lib/sessionRoute';
import { useHashRoute } from '../lib/useHashRoute';

// Always-mounted background component. Owns the single polling
// subscription to ListSessions, mirrors the result into sessionsStore
// so multiple UI consumers can read without each firing their own
// poll, and auto-selects the newest session the first time the picker
// has no selection.
export function SessionsSyncer() {
  const hash = useHashRoute();
  const filter = useMemo(() => sessionFilterRouteFromHash(hash), [hash]);
  const { sessions, loading, error } = useSessions(filter);
  const setSessions = useSessionsStore((s) => s.setSessions);
  const currentSessionId = useUiStore((s) => s.currentSessionId);
  const setCurrentSession = useUiStore((s) => s.setCurrentSession);

  useEffect(() => {
    setSessions(sessions, error, loading);
  }, [sessions, error, loading, setSessions]);

  useEffect(() => {
    // An invalid filter failed closed in useSessions (empty list + error);
    // never auto-select in that state.
    if (filter.kind === 'invalid') return;
    if (sessions.length === 0) return;
    if (currentSessionId && filter.kind === 'absent') return;
    if (currentSessionId && sessions.some((session) => session.id === currentSessionId)) {
      return;
    }
    let newest = sessions[0];
    let newestMs = sessionCreatedAtMs(newest);
    for (const s of sessions) {
      const ms = sessionCreatedAtMs(s);
      if (ms > newestMs) {
        newest = s;
        newestMs = ms;
      }
    }
    setCurrentSession(newest.id);
  }, [sessions, currentSessionId, filter, setCurrentSession]);

  return null;
}
