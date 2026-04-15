// Orchestration-event hook. Watches the SessionStore's SpanIndex and returns a
// live list of OrchestrationEvents derived from harmonograf reporting-tool
// TOOL_CALL spans. Reuses the store that useSessionWatch / GanttView already
// keeps alive — this hook does not open its own watch. Components that call
// it must be inside a tree where something else is holding the session watch
// (Shell's GanttView does this for the current session).

import { useEffect, useReducer, useMemo } from 'react';
import { getSessionStore } from './hooks';
import type { OrchestrationEvent } from '../gantt/index';

export function useOrchestrationEvents(
  sessionId: string | null,
  limit = 200,
): OrchestrationEvent[] {
  const store = getSessionStore(sessionId);
  const [version, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    if (!store) return;
    return store.spans.subscribe(() => bump());
  }, [store]);
  return useMemo(() => {
    if (!store) return [];
    void version;
    return store.listOrchestrationEvents(limit);
  }, [store, version, limit]);
}
