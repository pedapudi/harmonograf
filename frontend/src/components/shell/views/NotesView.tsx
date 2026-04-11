import './views.css';
import { useUiStore } from '../../../state/uiStore';
import { useAnnotationStore, type Annotation } from '../../../state/annotationStore';

// Stable reference so the zustand selector doesn't return a fresh array every
// render when the session has no annotations (which triggers React 19's
// useSyncExternalStore "getSnapshot should be cached" infinite loop and
// unmounts the entire tree).
const EMPTY_ANNOTATIONS: readonly Annotation[] = Object.freeze([]);

export function NotesView() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const selectSpan = useUiStore((s) => s.selectSpan);
  const annotations = useAnnotationStore(
    (s) => (sessionId && s.bySession.get(sessionId)) || EMPTY_ANNOTATIONS,
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
