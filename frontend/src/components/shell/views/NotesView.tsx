import './views.css';
import { useUiStore } from '../../../state/uiStore';
import { useAnnotationStore } from '../../../state/annotationStore';

export function NotesView() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const annotations = useAnnotationStore((s) =>
    sessionId ? (s.bySession.get(sessionId) ?? []) : [],
  );

  const sorted = annotations.slice().sort((a, b) => b.createdAtMs - a.createdAtMs);

  return (
    <section className="hg-panel" data-testid="notes-view">
      <header className="hg-panel__header">
        <h2 className="hg-panel__title">Notes</h2>
        <span className="hg-panel__hint">{sorted.length} annotation(s)</span>
      </header>
      <div className="hg-panel__body">
        {!sessionId && (
          <div className="hg-panel__empty">
            No session selected. Open the session picker (⌘K) to pick one.
          </div>
        )}
        {sessionId && sorted.length === 0 && (
          <div className="hg-panel__empty">No notes yet for this session.</div>
        )}
        {sorted.length > 0 && (
          <ul className="hg-notes__list" data-testid="notes-list">
            {sorted.map((a) => (
              <li
                key={a.id}
                className="hg-notes__row"
                data-testid="notes-row"
                data-annotation-id={a.id}
                onClick={() => a.spanId && selectSpan(a.spanId)}
              >
                <div className="hg-notes__meta">
                  <span className="hg-notes__author">{a.author}</span>
                  <span className="hg-notes__kind">{a.kind}</span>
                  {a.pending && <span className="hg-notes__pending">pending</span>}
                  {a.error && <span className="hg-notes__error">error</span>}
                </div>
                <div className="hg-notes__body">{a.body}</div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
