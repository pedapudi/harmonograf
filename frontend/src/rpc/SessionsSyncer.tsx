import { useEffect, useMemo } from 'react';
import { useSessions } from './hooks';
import { useSessionsStore, sessionCreatedAtMs } from '../state/sessionsStore';
import { useUiStore } from '../state/uiStore';
import { sessionMetadataFilterFromHash } from '../lib/sessionRoute';
import { useHashRoute } from '../lib/useHashRoute';

// Always-mounted background component. Owns the single polling
// subscription to ListSessions, mirrors the result into sessionsStore
// so multiple UI consumers can read without each firing their own
// poll, and auto-selects the newest session the first time the picker
// has no selection.
export function SessionsSyncer() {
  const hash = useHashRoute();
  const metadataFilter = useMemo(() => sessionMetadataFilterFromHash(hash), [hash]);
  const { sessions, loading, error } = useSessions(metadataFilter);
  const setSessions = useSessionsStore((s) => s.setSessions);
  const currentSessionId = useUiStore((s) => s.currentSessionId);
  const setCurrentSession = useUiStore((s) => s.setCurrentSession);

  useEffect(() => {
    setSessions(sessions, error, loading);
  }, [sessions, error, loading, setSessions]);

  useEffect(() => {
    if (sessions.length === 0) return;
    if (currentSessionId && Object.keys(metadataFilter).length === 0) return;
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
  }, [sessions, currentSessionId, metadataFilter, setCurrentSession]);

  return null;
}
