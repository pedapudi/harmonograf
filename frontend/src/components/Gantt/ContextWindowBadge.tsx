import { useCallback, useSyncExternalStore } from 'react';
import type { SessionStore } from '../../gantt';
import {
  contextColorForRatio,
  contextRatio,
  formatTokens,
} from '../../gantt/contextOverlay';

// Compact per-agent header chip showing current context window usage as a
// mini bar + tokens/limit text. The Gantt canvas renders the *history* band
// inside each row; this DOM chip renders the *current snapshot* so the user
// can see the latest ratio at a glance without scanning the sparkline.
//
// Subscribes to SessionStore.contextSeries directly and keeps its own small
// state — no parent rerender required when new samples arrive.

interface Props {
  store: SessionStore;
  agentId: string;
  // Compact mode drops the text labels to fit into tight agent-row headers.
  compact?: boolean;
}

export function ContextWindowBadge({ store, agentId, compact = false }: Props) {
  const subscribe = useCallback(
    (notify: () => void) =>
      store.contextSeries.subscribe((changedAgentId) => {
        if (changedAgentId === '' || changedAgentId === agentId) notify();
      }),
    [store, agentId],
  );
  const getSnapshot = useCallback(
    () => store.contextSeries.latest(agentId),
    [store, agentId],
  );
  const latest = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  if (!latest || latest.limitTokens <= 0) return null;

  const ratio = contextRatio(latest.tokens, latest.limitTokens);
  const color = contextColorForRatio(ratio);
  const pct = Math.round(ratio * 100);
  const barWidth = compact ? 36 : 52;
  const label = `${formatTokens(latest.tokens)} / ${formatTokens(latest.limitTokens)}`;

  return (
    <span
      data-testid={`ctxwin-badge-${agentId}`}
      role="status"
      aria-label={`context window ${color.bucket}: ${latest.tokens} of ${latest.limitTokens} tokens (${pct}%)`}
      title={`${label} (${pct}%)`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 10,
        fontFamily: 'system-ui, sans-serif',
        color: 'var(--md-sys-color-on-surface-variant, #c3c6cf)',
        padding: '1px 6px',
        borderRadius: 999,
        background: 'var(--md-sys-color-surface-container-high, #262931)',
        border: '1px solid var(--md-sys-color-outline-variant, #43474e)',
        whiteSpace: 'nowrap',
      }}
    >
      <span
        aria-hidden
        style={{
          position: 'relative',
          width: barWidth,
          height: 6,
          borderRadius: 3,
          background: 'var(--md-sys-color-surface-variant, #43474e)',
          overflow: 'hidden',
          flex: '0 0 auto',
        }}
      >
        <span
          style={{
            position: 'absolute',
            inset: 0,
            width: `${Math.max(2, pct)}%`,
            background: color.stroke,
            borderRadius: 3,
          }}
        />
      </span>
      {!compact && <span>{label}</span>}
      <span style={{ opacity: 0.75 }}>{pct}%</span>
    </span>
  );
}
