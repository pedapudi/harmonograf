import { useEffect, useMemo, useRef, useState } from 'react';
import type { SessionStore } from '../../gantt/index';
import { useUiStore } from '../../state/uiStore';
import { useSendControl } from '../../rpc/hooks';

// Rising MD3-style snackbar for AWAITING_HUMAN spans. Doc 04 §7.4.
//
// We watch the SessionStore for spans transitioning into AWAITING_HUMAN,
// queue them FIFO, and surface the head of the queue. Approving/Rejecting
// from here dispatches SendControl directly — the server transitions the
// span back to RUNNING and we dequeue on the next tick (the watcher sees
// the status flip and drops it from the queue).

interface Props {
  store: SessionStore;
  sessionId: string;
}

export function AttentionSnackbar({ store, sessionId }: Props) {
  const [queue, setQueue] = useState<string[]>([]);
  const seen = useRef<Set<string>>(new Set());
  const selectSpan = useUiStore((s) => s.selectSpan);
  const send = useSendControl();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Poll the span index for AWAITING_HUMAN spans on every animation frame.
  // Cheaper than subscribing to every dirty rect — the set of awaiting spans
  // is tiny in practice (at most a handful at a time).
  useEffect(() => {
    let handle = 0;
    const tick = () => {
      const awaiting: string[] = [];
      for (const agent of store.agents.list) {
        const spans = store.spans.queryAgent(
          agent.id,
          -Number.MAX_SAFE_INTEGER,
          Number.MAX_SAFE_INTEGER,
        );
        for (const s of spans) {
          if (s.status === 'AWAITING_HUMAN') awaiting.push(s.id);
        }
      }
      const awaitingSet = new Set(awaiting);
      setQueue((prev) => {
        const next = prev.filter((id) => awaitingSet.has(id));
        for (const id of awaiting) {
          if (!seen.current.has(id)) {
            seen.current.add(id);
            next.push(id);
          }
        }
        // GC the seen set when spans leave awaiting to allow re-enqueueing on
        // re-enter (e.g. after a rejected + re-proposed action).
        for (const id of Array.from(seen.current)) {
          if (!awaitingSet.has(id)) seen.current.delete(id);
        }
        return next;
      });
      handle = requestAnimationFrame(tick);
    };
    handle = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(handle);
  }, [store]);

  const head = queue[0];
  const span = useMemo(
    () => (head ? store.spans.get(head) : null),
    [head, store],
  );

  if (!span) return null;

  const toolName =
    span.attributes['tool.name']?.kind === 'string'
      ? (span.attributes['tool.name'] as { kind: 'string'; value: string }).value
      : span.name;
  const encoder = new TextEncoder();

  const dispatch = async (kind: 'APPROVE' | 'REJECT') => {
    setBusy(kind);
    setError(null);
    try {
      await send({
        sessionId,
        agentId: span.agentId,
        spanId: span.id,
        kind,
        payload: kind === 'REJECT' ? encoder.encode('rejected via snackbar') : undefined,
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: 'fixed',
        left: '50%',
        bottom: 24,
        transform: 'translateX(-50%)',
        minWidth: 420,
        maxWidth: 600,
        background: 'var(--md-sys-color-inverse-surface, #2e3036)',
        color: 'var(--md-sys-color-inverse-on-surface, #eff0f7)',
        borderRadius: 8,
        boxShadow: '0 8px 24px rgba(0,0,0,0.45)',
        padding: '12px 16px',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        zIndex: 2000,
        fontSize: 13,
      }}
    >
      <span
        aria-hidden="true"
        style={{
          width: 10,
          height: 10,
          borderRadius: '50%',
          background: 'var(--md-sys-color-error, #ffb4ab)',
          flexShrink: 0,
          boxShadow: '0 0 0 4px rgba(255,180,171,0.25)',
        }}
      />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          Agent {span.agentId} needs your input: {toolName}
        </div>
        {error && (
          <div style={{ color: 'var(--md-sys-color-error, #ffb4ab)', marginTop: 4 }}>
            {error}
          </div>
        )}
        {queue.length > 1 && (
          <div style={{ opacity: 0.7, marginTop: 2 }}>
            +{queue.length - 1} more pending
          </div>
        )}
      </div>
      <button
        onClick={() => selectSpan(span.id)}
        style={buttonStyle}
        disabled={busy !== null}
      >
        Inspect
      </button>
      <button
        onClick={() => void dispatch('REJECT')}
        style={buttonStyle}
        disabled={busy !== null}
      >
        {busy === 'REJECT' ? '…' : 'Reject'}
      </button>
      <button
        onClick={() => void dispatch('APPROVE')}
        style={{ ...buttonStyle, fontWeight: 700 }}
        disabled={busy !== null}
      >
        {busy === 'APPROVE' ? '…' : 'Approve'}
      </button>
    </div>
  );
}

const buttonStyle: React.CSSProperties = {
  background: 'transparent',
  color: 'inherit',
  border: '1px solid currentColor',
  borderRadius: 999,
  padding: '4px 12px',
  cursor: 'pointer',
  fontSize: 12,
};
