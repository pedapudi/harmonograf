import { useEffect, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import { formatDuration } from '../../lib/format';

// Capabilities are per-agent in the real model; until task #12 wires real
// session data, treat this scaffold as if all transport buttons are supported.
const CAPS = {
  rewind: true,
  pauseResume: true,
  cancel: true,
  step: true,
};

export function TransportBar() {
  const paused = useUiStore((s) => s.paused);
  const togglePause = useUiStore((s) => s.togglePause);
  const zoomIn = useUiStore((s) => s.zoomIn);
  const zoomOut = useUiStore((s) => s.zoomOut);
  const zoomSeconds = useUiStore((s) => s.zoomSeconds);
  const liveFollow = useUiStore((s) => s.liveFollow);
  const sessionId = useUiStore((s) => s.currentSessionId);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!sessionId) return;
    const i = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(i);
  }, [sessionId]);

  const elapsedSeconds = sessionId ? tick : 0;

  return (
    <footer className="hg-transport" role="toolbar" aria-label="Transport controls">
      <div className="hg-transport__group">
        <button
          className="hg-transport__btn"
          disabled={!CAPS.rewind || !sessionId}
          aria-label="Rewind to selected"
          title="Rewind to selected"
        >
          ⏮
        </button>
        <button
          className="hg-transport__btn"
          disabled={!CAPS.pauseResume || !sessionId}
          aria-label={paused ? 'Resume' : 'Pause'}
          title={paused ? 'Resume' : 'Pause'}
          onClick={togglePause}
        >
          {paused ? '▶' : '⏸'}
        </button>
        <button
          className="hg-transport__btn"
          disabled={!CAPS.step || !paused}
          aria-label="Step"
          title="Step (paused only)"
        >
          ⏭
        </button>
        <button
          className="hg-transport__btn"
          disabled={!CAPS.cancel || !sessionId}
          aria-label="Stop"
          title="Stop"
        >
          ⏹
        </button>
      </div>
      <div className="hg-transport__clock">
        {liveFollow && sessionId && <span className="hg-transport__live-dot" />}
        {sessionId ? formatDuration(elapsedSeconds) : '—'}
        {sessionId && <span> / {liveFollow ? 'LIVE' : 'PAUSED'}</span>}
      </div>
      <div className="hg-transport__spacer" />
      <div className="hg-transport__group">
        <span className="hg-picker__row-sub" aria-label="Zoom window">
          {formatDuration(zoomSeconds)} window
        </span>
        <button
          className="hg-transport__btn"
          onClick={zoomOut}
          aria-label="Zoom out"
          title="Zoom out (-)"
        >
          −
        </button>
        <button
          className="hg-transport__btn"
          onClick={zoomIn}
          aria-label="Zoom in"
          title="Zoom in (+)"
        >
          +
        </button>
      </div>
    </footer>
  );
}
