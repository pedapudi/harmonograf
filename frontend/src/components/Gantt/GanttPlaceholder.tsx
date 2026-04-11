import { useUiStore } from '../../state/uiStore';

export function GanttPlaceholder() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const selectSpan = useUiStore((s) => s.selectSpan);

  return (
    <div className="hg-gantt-placeholder">
      <div className="hg-gantt-placeholder__gutter">Agent rows</div>
      <div style={{ paddingLeft: 200 }}>
        {sessionId ? (
          <button
            className="hg-appbar__session-trigger"
            onClick={() => selectSpan('demo-span-1')}
          >
            Click to test span selection (Gantt arrives in task #11)
          </button>
        ) : (
          <span>No session selected. Open the picker (⌘K).</span>
        )}
      </div>
    </div>
  );
}
