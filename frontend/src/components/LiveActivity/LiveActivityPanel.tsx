import { useEffect, useState } from 'react';
import { useUiStore } from '../../state/uiStore';
import { useSessionWatch } from '../../rpc/hooks';
import { colorForAgent } from '../../theme/agentColors';
import { formatDuration } from '../../lib/format';
import type { Span, AttributeValue } from '../../gantt/types';
import './LiveActivity.css';

interface ActiveItem {
  agentId: string;
  agentName: string;
  color: string;
  taskReport: string;
  hasThinking: boolean;
  elapsed: number;
  invocationId: string;
}

function attrToString(v: AttributeValue | undefined): string {
  if (!v) return '';
  switch (v.kind) {
    case 'string':
      return v.value;
    case 'int':
      return v.value.toString();
    case 'double':
      return String(v.value);
    case 'bool':
      return v.value ? 'true' : 'false';
    default:
      return '';
  }
}

function attrToBool(v: AttributeValue | undefined): boolean {
  if (!v) return false;
  if (v.kind === 'bool') return v.value;
  if (v.kind === 'string') return v.value.length > 0;
  return false;
}

export function LiveActivityPanel() {
  const sessionId = useUiStore((s) => s.currentSessionId);
  const watch = useSessionWatch(sessionId);
  const liveActivityCollapsed = useUiStore((s) => s.liveActivityCollapsed);
  const toggle = useUiStore((s) => s.toggleLiveActivity);
  const [, setTick] = useState(0);
  // Wall-clock "now" is sampled inside the interval (an external-system
  // callback) rather than during render, since Date.now is impure. First
  // render shows 0 until the interval populates this; the next tick corrects
  // it.
  const [nowWallMs, setNowWallMs] = useState(0);

  // Re-render every second to keep elapsed timers ticking, and also subscribe
  // to span/agent changes so new thinking fragments appear immediately.
  useEffect(() => {
    const id = window.setInterval(() => {
      setNowWallMs(Date.now());
      setTick((t) => t + 1);
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!watch?.store) return;
    const unSpans = watch.store.spans.subscribe(() => setTick((t) => t + 1));
    const unAgents = watch.store.agents.subscribe(() => setTick((t) => t + 1));
    return () => {
      unSpans();
      unAgents();
    };
  }, [watch?.store]);

  if (!sessionId || !watch?.store) return null;

  const store = watch.store;
  const items: ActiveItem[] = [];

  // Find all running INVOCATION spans across agents.
  const allSpans = store.spans.queryRange(
    -Number.MAX_SAFE_INTEGER,
    Number.MAX_SAFE_INTEGER,
  );
  const runningByAgent = new Map<string, Span>();
  for (const s of allSpans) {
    if (s.kind !== 'INVOCATION' || s.endMs !== null) continue;
    const prev = runningByAgent.get(s.agentId);
    if (!prev || s.startMs > prev.startMs) runningByAgent.set(s.agentId, s);
  }

  // Reference time for elapsed calculations. In live mode the renderer keeps
  // store.nowMs advancing; fall back to wall-clock math if it hasn't been set.
  const nowRel =
    store.nowMs > 0
      ? store.nowMs
      : store.wallClockStartMs > 0 && nowWallMs > 0
        ? nowWallMs - store.wallClockStartMs
        : 0;

  for (const [agentId, span] of runningByAgent) {
    const agent = store.agents.get(agentId);
    if (!agent) continue;
    const attrReport = attrToString(span.attributes['task_report']);
    const taskReport = attrReport || agent.taskReport || '';
    const hasThinking =
      attrToBool(span.attributes['has_thinking']) ||
      taskReport.startsWith('Thinking');
    const elapsedMs = Math.max(0, nowRel - span.startMs);
    items.push({
      agentId,
      agentName: agent.name,
      color: colorForAgent(agentId),
      taskReport,
      hasThinking,
      elapsed: Math.floor(elapsedMs / 1000),
      invocationId: span.id,
    });
  }

  // Sort by elapsed descending (longest-running first).
  items.sort((a, b) => b.elapsed - a.elapsed);

  if (items.length === 0) {
    return (
      <div className="hg-live-activity hg-live-activity--empty">
        <div className="hg-live-activity__header">
          <span className="hg-live-activity__title">
            <span className="hg-live-activity__idle-dot" />
            No active work
          </span>
        </div>
      </div>
    );
  }

  return (
    <div
      className={`hg-live-activity${liveActivityCollapsed ? ' hg-live-activity--collapsed' : ''}`}
    >
      <div
        className="hg-live-activity__header"
        onClick={toggle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') toggle();
        }}
      >
        <span className="hg-live-activity__title">
          <span className="hg-live-activity__pulse-dot" />
          Live Activity &middot; {items.length} running
        </span>
        <span className="hg-live-activity__chevron">
          {liveActivityCollapsed ? '\u25B8' : '\u25BE'}
        </span>
      </div>
      {!liveActivityCollapsed && (
        <div className="hg-live-activity__cards">
          {items.map((item) => (
            <LiveActivityCard key={item.agentId} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}

function LiveActivityCard({ item }: { item: ActiveItem }) {
  const select = useUiStore((s) => s.selectSpan);
  return (
    <div
      className="hg-live-activity__card"
      style={{ borderLeftColor: item.color, color: item.color }}
      onClick={() => select(item.invocationId)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') select(item.invocationId);
      }}
    >
      <div className="hg-live-activity__card-header">
        <span
          className="hg-live-activity__card-dot"
          style={{ background: item.color }}
        />
        <span className="hg-live-activity__card-name">{item.agentName}</span>
        <span className="hg-live-activity__card-elapsed">
          {formatDuration(item.elapsed)}
        </span>
      </div>
      <div className="hg-live-activity__card-task">
        {item.hasThinking && (
          <span className="hg-live-activity__card-thinking">&#128173; </span>
        )}
        {item.taskReport || (
          <span style={{ opacity: 0.5 }}>Waiting for response&hellip;</span>
        )}
      </div>
    </div>
  );
}
