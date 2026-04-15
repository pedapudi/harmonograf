import { useEffect, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import { formatDuration } from '../../lib/format';
import { useSessionWatch, useSendControl } from '../../rpc/hooks';

export function TransportBar() {
  const liveFollow = useUiStore((s) => s.liveFollow);
  const toggleLiveFollow = useUiStore((s) => s.toggleLiveFollow);
  const jumpToLive = useUiStore((s) => s.jumpToLive);
  const agentsPaused = useUiStore((s) => s.agentsPaused);
  const setAgentsPaused = useUiStore((s) => s.setAgentsPaused);
  const zoomIn = useUiStore((s) => s.zoomIn);
  const zoomOut = useUiStore((s) => s.zoomOut);
  const zoomSeconds = useUiStore((s) => s.zoomSeconds);
  const sessionId = useUiStore((s) => s.currentSessionId);
  const [tick, setTick] = useState(0);
  // Reset elapsed counter when session changes. This is the React-recommended
  // pattern for resetting state on prop change — setState during render beats
  // resetting inside an effect, which would trigger a cascading render.
  const [prevSessionId, setPrevSessionId] = useState(sessionId);
  if (prevSessionId !== sessionId) {
    setPrevSessionId(sessionId);
    setTick(0);
  }

  const send = useSendControl();
  const watch = useSessionWatch(sessionId);
  const agents = watch.store.agents.list;

  useEffect(() => {
    if (!sessionId) return;
    const i = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(i);
  }, [sessionId]);

  const elapsedSeconds = sessionId ? tick : 0;

  const handlePauseAgents = async () => {
    if (!sessionId) return;
    // Freeze renderer and update store state first (instant feedback).
    setAgentsPaused(true);
    // Best-effort: send PAUSE to all currently known agents.
    for (const agent of agents) {
      await send({
        sessionId,
        agentId: agent.id,
        kind: 'PAUSE',
      }).catch(() => {});
    }
  };

  const handleResumeAgents = async () => {
    if (!sessionId) return;
    // Unfreeze renderer and update store state first (instant feedback).
    setAgentsPaused(false);
    // Re-enable viewport live follow so the Gantt catches back up.
    jumpToLive();
    // Best-effort: send RESUME to all currently known agents.
    for (const agent of agents) {
      await send({
        sessionId,
        agentId: agent.id,
        kind: 'RESUME',
      }).catch(() => {});
    }
  };

  return (
    <footer
      className="hg-transport"
      role="toolbar"
      aria-label="Transport controls"
      data-testid="transport-bar"
    >
      {/* Agent controls — rewind + stop (future: wired to SendControl) */}
      <div className="hg-transport__group">
        <button
          className="hg-transport__btn"
          disabled={!sessionId}
          aria-label="Rewind to selected"
          title="Rewind to selected span"
        >
          ⏮
        </button>
        <button
          className="hg-transport__btn"
          disabled={!sessionId}
          aria-label="Stop"
          title="Stop session"
        >
          ⏹
        </button>
      </div>

      <div className="hg-transport__divider" />

      {/* Live / paused status + clock + pause/resume */}
      <div className="hg-transport__live-group">
        {agentsPaused ? (
          <>
            <span className="hg-transport__paused-badge" style={{ color: '#f59e0b' }}>
              ⏸ AGENTS PAUSED
            </span>
            <span className="hg-transport__clock" aria-label="Elapsed time">
              {sessionId ? formatDuration(elapsedSeconds) : '—'}
            </span>
            <button
              className="hg-transport__btn"
              onClick={handleResumeAgents}
              disabled={!sessionId}
              aria-label="Resume agents"
              title="Resume agent execution and return to live"
            >
              ▶ Resume
            </button>
          </>
        ) : (
          <>
            {liveFollow && sessionId ? (
              <span className="hg-transport__live-badge">
                <span className="hg-transport__live-dot" aria-hidden="true" />
                LIVE
              </span>
            ) : (
              <span className="hg-transport__paused-badge">○ Viewport locked</span>
            )}

            <span className="hg-transport__clock" aria-label="Elapsed time">
              {sessionId ? formatDuration(elapsedSeconds) : '—'}
            </span>

            <button
              className="hg-transport__btn hg-transport__pause-btn"
              onClick={handlePauseAgents}
              disabled={!sessionId}
              aria-label="Pause agents"
              title="Pause all agents at next model boundary"
            >
              ⏸
            </button>

            {/* Viewport follow toggle — only shown when not following live */}
            {!liveFollow && (
              <button
                className="hg-transport__btn hg-transport__return-live-btn"
                onClick={toggleLiveFollow}
                disabled={!sessionId}
                aria-label="Return to live"
                title="Return viewport to live edge"
              >
                ↩ Follow live
              </button>
            )}
          </>
        )}
      </div>

      <div className="hg-transport__spacer" />

      {/* Zoom controls */}
      <div className="hg-transport__group">
        <span className="hg-transport__zoom-label" aria-label="Zoom window">
          {formatDuration(zoomSeconds)} window
        </span>
        <button
          className="hg-transport__btn"
          onClick={zoomOut}
          aria-label="Zoom out"
          title="Zoom out (wider window)"
        >
          −
        </button>
        <button
          className="hg-transport__btn"
          onClick={zoomIn}
          aria-label="Zoom in"
          title="Zoom in (narrower window)"
        >
          +
        </button>
      </div>
    </footer>
  );
}
