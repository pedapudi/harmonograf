import { useEffect } from 'react';
import { useSessions } from './hooks';
import { useSessionsStore, sessionCreatedAtMs } from '../state/sessionsStore';
import { useUiStore } from '../state/uiStore';

// Always-mounted background component. Owns the single polling
// subscription to ListSessions, mirrors the result into sessionsStore
// so multiple UI consumers can read without each firing their own
// poll, and auto-selects the newest session the first time the picker
// has no selection.
export function SessionsSyncer() {
  const { sessions, loading, error } = useSessions();
  const setSessions = useSessionsStore((s) => s.setSessions);
  const currentSessionId = useUiStore((s) => s.currentSessionId);
  const setCurrentSession = useUiStore((s) => s.setCurrentSession);

  useEffect(() => {
    setSessions(sessions, error, loading);
  }, [sessions, error, loading, setSessions]);

  useEffect(() => {
    if (currentSessionId) return;
    if (sessions.length === 0) return;
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
  }, [sessions, currentSessionId, setCurrentSession]);

  return null;
}
