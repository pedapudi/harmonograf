import './views.css';
import { useEffect, useState } from 'react';
import { useUiStore } from '../../../state/uiStore';
import { useSessionWatch } from '../../../rpc/hooks';
import type { Span } from '../../../gantt/types';

const MAX_ROWS = 200;

function fmtTime(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r.toString().padStart(2, '0')}`;
}

export function ActivityView() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const watch = useSessionWatch(sessionId);
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!sessionId) return;
    return watch.store.spans.subscribe(() => setTick((n) => n + 1));
  }, [sessionId, watch.store]);

  let recent: Span[] = [];
  if (sessionId) {
    const all = watch.store.spans.queryRange(
      -Number.MAX_SAFE_INTEGER,
      Number.MAX_SAFE_INTEGER,
    );
    recent = all
      .slice()
      .sort((a, b) => b.startMs - a.startMs)
      .slice(0, MAX_ROWS);
  }

  return (
    <section className="hg-panel" data-testid="activity-view">
      <header className="hg-panel__header">
        <h2 className="hg-panel__title">Activity</h2>
        <span className="hg-panel__hint">{recent.length} recent span(s)</span>
      </header>
      <div className="hg-panel__body">
        {!sessionId && (
          <div className="hg-panel__empty">
            No session selected. Open the session picker (⌘K) to pick one.
          </div>
        )}
        {sessionId && recent.length === 0 && (
          <div className="hg-panel__empty">No activity yet for this session.</div>
        )}
        {sessionId && recent.length > 0 && (
          <ul className="hg-activity__list" data-testid="activity-list">
            {recent.map((span) => (
              <li
                key={span.id}
                className="hg-activity__row"
                data-testid="activity-row"
                data-span-id={span.id}
                onClick={() => selectSpan(span.id)}
              >
                <span className="hg-activity__time">{fmtTime(span.startMs)}</span>
                <span className="hg-activity__kind">{span.kind}</span>
                <span className="hg-activity__name">{span.name || '(unnamed)'}</span>
                <span className="hg-activity__agent">{span.agentId}</span>
                <span
                  className="hg-activity__status"
                  data-status={span.status}
                >
                  {span.status}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
